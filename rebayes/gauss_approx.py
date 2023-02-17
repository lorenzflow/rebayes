"""
Implementation of the Recursive Variational Gaussian Approximation
(R-VGA) and the Limited-memory Recursive Variational Gaussian Approximation
(LR-VGA) [1] algorithms for sequential estimation.

[1] Lambert, M., Bonnabel, S., & Bach, F. (2021, December).
The limited-memory recursive variational Gaussian approximation (L-RVGA).
Retrieved from https://hal.inria.fr/hal-03501920
"""

import jax
import chex
import flax.linen as nn
import jax.numpy as jnp
from typing import Callable
from functools import partial
from dataclasses import dataclass
from jaxtyping import Array, Float
from jax.flatten_util import ravel_pytree
from rebayes.base import Rebayes
from flax import struct


@chex.dataclass
class LRVGAState:
    mu: Float[Array, "dim_params"]
    W: Float[Array, "dim_params dim_subspace"]
    Psi: Float[Array, "dim_params"]


# Homoskedastic case (we estimate sigma at a warmup stage)
@struct.dataclass
class FlaxLRVGAState:
    key: jax.random.PRNGKey
    mu: Float[Array, "dim_params"]
    W: Float[Array, "dim_params dim_subspace"]
    Psi: Float[Array, "dim_params"]
    sigma: float = 1.0
    step: int = 0

    @property
    def mean(self):
        return self.mu


@dataclass
class Config:
    """
    Static component of the LRVGA algorithm.

    Parameters
    ----------
    num_samples
        Number of samples to draw from the variational Gaussian approx
    dim_latent
        Dimensionality of the latent subspace
    model
        Flax model to use for the variational Gaussian approx
    reconstruct_fn
        Reconstructs the model parameters from the flattened vector
    """
    num_samples: int
    dim_latent: int
    model: nn.Module
    reconstruct_fn: Callable


def init_state_lrvga(key, model, X, dim_latent, sigma2_init, num_samples, eps):
    key_W, key_mu, key_carry = jax.random.split(key, 3)

    mu_init = model.init(key_mu, X)
    mu_init, reconstruct_fn = ravel_pytree(mu_init)
    mu_init = jnp.array(mu_init)
    dim_params = len(mu_init)

    psi0 = (1 - eps) / sigma2_init
    w0 = jnp.sqrt((eps * dim_params) / (dim_latent * sigma2_init))
    
    W_init = jax.random.normal(key_W, (dim_params, dim_latent))
    W_init = W_init / jnp.linalg.norm(W_init, axis=0) * w0
    Psi_init = jnp.ones(dim_params) * psi0
    
    state_init = FlaxLRVGAState(
        key=key_carry,
        mu=mu_init,
        W=W_init,
        Psi=Psi_init,
    )

    config = Config(
        dim_latent=dim_latent,
        model=model,
        reconstruct_fn=reconstruct_fn,
        num_samples=num_samples,
    )
    
    return state_init, config


def fa_approx_step(
    x: Float[Array, "dim_params"],
    state: LRVGAState,
    state_prev: LRVGAState,
    alpha: float,
    beta: float
) -> LRVGAState:
    """
    Factor Analysis (FA) approximation to the low-rank (W)
    and diagonal (Psi) matrices.
    """
    # Load data
    W_prev, Psi_prev = state_prev.W, state_prev.Psi
    W, Psi = state.W, state.Psi
    
    # Initialise basic transformations
    dim_obs, dim_latent = W.shape
    I = jnp.eye(dim_latent)
    Psi_inv = 1 / Psi
    
    # Construct helper matrices
    M = I + jnp.einsum("ij,i,ik->jk", W, Psi_inv, W)
    M_inv = jnp.linalg.inv(M)
    V_beta = jnp.einsum("...i,...j,j,jk->ik", x, x, Psi_inv, W)
    V_alpha = (
        jnp.einsum("ij,kj,k,kl->il", W_prev, W_prev, Psi_inv, W) +
        jnp.einsum("i,i,ij->ij", Psi_prev, Psi_inv, W)
    )
    V = beta * V_beta + alpha * V_alpha
    # Value_update
    # (return transpose of W_solve -- avoid extra transpose op)
    W_solve = I + jnp.einsum("ij,kj,k,kl->li", M_inv, W, Psi_inv, V)
    W = jnp.linalg.solve(W_solve, V.T).T
    Psi = (
        beta * jnp.einsum("...i,...i->i", x, x) +
        alpha * jnp.einsum("ij,ij->i", W_prev, W_prev) + 
        alpha * Psi_prev -
        jnp.einsum("ij,jk,ik->i", W, M_inv, V)
    )
    
    new_state = state.replace(
        mu=state.mu,
        W=W,
        Psi=Psi
    )
    return new_state


@jax.jit
def sample_lr_params(key, state):
    """
    Sample parameters from a low-rank variational Gaussian approximation.
    This implementation avoids the explicit construction of the
    (D x D) covariance matrix.

    We take s ~ N(0, W W^T + Psi I)

    Implementation based on §4.2.2 of the L-RVGA paper.

    TODO(?): refactor code into jax.vmap. (It faster?)
    """
    key_x, key_eps = jax.random.split(key)
    dim_full, dim_latent = state.W.shape
    Psi_inv = 1 / state.Psi

    eps_sample = jax.random.normal(key_eps, (dim_latent,))
    x_sample = jax.random.normal(key_x, (dim_full,)) * jnp.sqrt(Psi_inv)

    I_full = jnp.eye(dim_full)
    I_latent = jnp.eye(dim_latent)
    # M = I + W^T Psi^{-1} W
    M = I_latent + jnp.einsum("ji,j,jk->ik", state.W, Psi_inv, state.W)
    # L = Psi^{-1} W^T M^{-1}
    L_tr = jnp.linalg.solve(M.T, jnp.einsum("i,ij->ji", Psi_inv, state.W))

    # samples = (I - LW^T)x + Le
    term1 = I_full - jnp.einsum("ji,kj->ik", L_tr, state.W)
    x_transform = jnp.einsum("ij,j->i", term1, x_sample)
    eps_transform = jnp.einsum("ji,j->i", L_tr, eps_sample)
    samples = x_transform + eps_transform
    return samples + state.mu


@partial(jax.vmap, in_axes=(0, None, None, None, None))
def sample_predictions(key, state, x, model, reconstruct_fn):
    mu_sample = sample_lr_params(key, state)
    mu_sample = reconstruct_fn(mu_sample)
    yhat = model.apply(mu_sample, x, method=model.get_mean)
    return yhat


@partial(jax.vmap, in_axes=(0, None, None, None, None, None))
def sample_grad_expected_log_prob(key, state, x, y, model, reconstruct_fn):
    """
    E[∇ logp(y|x,θ)]
    """
    mu_sample = sample_lr_params(key, state)
    mu_sample = reconstruct_fn(mu_sample)
    grad_log_prob = partial(model.apply, method=model.log_prob)
    grad_log_prob = jax.grad(grad_log_prob, argnums=0)
    grads = grad_log_prob(mu_sample, x, y)
    grads, _ = ravel_pytree(grads)
    return grads


def mu_update(
    key,
    x: Float[Array, "dim_obs"],
    y: float,
    state_prev: LRVGAState,
    state: LRVGAState,
    num_samples: int,
    model: nn.Module,
    reconstruct_fn: Callable,
) -> Float[Array, "dim_obs"]:
    """
    TODO: Optimise for lower compilation time:
        1. Refactor sample_predictions
        2. Refactor sample_grad_expected_log_prob
    TODO: Rewrite the V term using the Woodbury matrix identity
    """
    W = state.W
    Psi_inv = 1 / state.Psi
    dim_full, _ = W.shape
    I = jnp.eye(dim_full)

    keys_grad = jax.random.split(key, num_samples)

    V = W @ W.T + state.Psi * I
    exp_grads_log_prob = sample_grad_expected_log_prob(keys_grad, state_prev, x, y, model, reconstruct_fn).mean(axis=0)
    gain = jnp.linalg.solve(V, exp_grads_log_prob)
    return gain


def fwd_link(params, x, model, reconstruct_fn):
    """
    TODO: Generalise to any member of the exponential family

    Returns
    -------
    * predicted mean and logvar
    * predicted standard deviation
    """
    params = reconstruct_fn(params)
    # TODO: rewrite for possibly multivariate, heteroskedastic model
    nparams = model.apply(params, x).ravel()
    std = model.std
    return nparams, std


def get_coef(params, x, model, reconstruct_fn):
    c, std = jax.jacfwd(fwd_link, has_aux=True)(params, x, model, reconstruct_fn)
    return c * std


@partial(jax.vmap, in_axes=(0, None, None, None, None))
def sample_cov_coeffs(key, x, state, model, reconstruct_fn):
    params = sample_lr_params(key, state)
    coef = get_coef(params, x, model, reconstruct_fn)
    return coef


@partial(jax.jit, static_argnames=("num_samples", "model", "reconstruct_fn"))
def sample_half_fisher(key, x, state, num_samples, model, reconstruct_fn):
    """
    Estimate X such that
        X X^T ~ E_{q(θ)}[E_{y}[∇^2 log p(y|x,θ)]]
    """
    keys = jax.random.split(key, num_samples)
    coeffs = sample_cov_coeffs(keys, x, state, model, reconstruct_fn) / jnp.sqrt(num_samples)
    # XXtr = jnp.einsum("nji,njk->ik", coeffs, coeffs) / num_samples
    return coeffs


def _step_lrvga(state, obs, alpha, beta, n_inner, n_samples, model, reconstruct_fn):
    """
    Iterated RVGA (§4.2.1). We omit the second iteration of the covariance matrix
    """
    key, x, y = obs
    key_fisher, key_est, key_mu_final = jax.random.split(key, 3)

    X = sample_half_fisher(key_fisher, x, state, n_samples, model, reconstruct_fn)
    def fa_partial(_, new_state):
        new_state = fa_approx_step(X, new_state, state, alpha, beta)
        return new_state

    # Algorithm 1 in §3.2 of L-RVGA states that 1 to 3 loops may be enough in
    # the inner (fa-update) loop
    state_update = jax.lax.fori_loop(0, n_inner, fa_partial, state)
    # First mu update
    mu_add = mu_update(key_est, x, y, state, state_update, n_samples, model, reconstruct_fn)
    mu_new = state.mu + mu_add
    state_update = state_update.replace(mu=mu_new)
    # Second mu update: we use the updated state to estimate the gradient
    mu_add = mu_update(key_mu_final, x, y, state_update, state_update, n_samples, model, reconstruct_fn)
    mu_new = state.mu + mu_add
    state_update = state_update.replace(mu=mu_new)
    return state_update, state_update.mu
    

def lrvga(
    key: jax.random.PRNGKey,
    state_init: LRVGAState,
    X: Float[Array, "num_obs dim_obs"],
    y: Float[Array, "num_obs"],
    alpha: float,
    beta: float,
    model: nn.Module,
    reconstruct_fn: Callable,
    n_inner: int = 3,
    n_inner_fa: int = 3,
    n_samples: int = 6
):
    n_steps = len(y)
    keys = jax.random.split(key, n_steps)
    part_lrvga = partial(
        _step_lrvga, alpha=alpha, beta=beta, n_inner=n_inner_fa, n_samples=n_samples,
        model=model, reconstruct_fn=reconstruct_fn
    )
    def run_lrvga(state, obs):
        state = jax.lax.fori_loop(0, n_inner, lambda _, state: part_lrvga(state, obs)[0], state)
        return state, state.mu

    obs = (keys, X, y)
    state_final, mu_hist = jax.lax.scan(run_lrvga, state_init, obs)
    return state_final, mu_hist


class LRVGA(Rebayes):
    """
    Limited-memory recursive variational Gaussian approximation (LRVGA)
    for a homoskedastic Gaussian model with known mean
    """
    def __init__(
            self,
            # rebayes_params: FlaxLRVGAState # bel
            fwd_link: Callable,
            log_prob: Callable,
            alpha: float = 1.0,
            beta: float = 1.0,
            n_inner: int = 3,
            n_inner_fa: int = 3,
            n_samples: int = 6
    ):
        self.fwd_link = fwd_link
        self.log_prob = log_prob
        self.alpha = alpha
        self.beta = beta
        self.n_inner = n_inner
        self.n_inner_fa = n_inner_fa
        self.n_samples = n_samples
        self.grad_log_prob = jax.grad(log_prob, argnums=0)

    @staticmethod
    def _sample_lr_params(key, bel):
        """
        Sample parameters from a low-rank variational Gaussian approximation.
        This implementation avoids the explicit construction of the
        (D x D) covariance matrix.

        We take s ~ N(0, W W^T + Psi I)

        Implementation based on §4.2.2 of the L-RVGA paper.

        TODO(?): refactor code into jax.vmap. (It faster?)
        """
        key_x, key_eps = jax.random.split(key)
        dim_full, dim_latent = bel.W.shape
        Psi_inv = 1 / bel.Psi

        eps_sample = jax.random.normal(key_eps, (dim_latent,))
        x_sample = jax.random.normal(key_x, (dim_full,)) * jnp.sqrt(Psi_inv)

        I_full = jnp.eye(dim_full)
        I_latent = jnp.eye(dim_latent)
        # M = I + W^T Psi^{-1} W
        M = I_latent + jnp.einsum("ji,j,jk->ik", bel.W, Psi_inv, bel.W)
        # L = Psi^{-1} W^T M^{-1}
        L_tr = jnp.linalg.solve(M.T, jnp.einsum("i,ij->ji", Psi_inv, bel.W))

        # samples = (I - LW^T)x + Le
        term1 = I_full - jnp.einsum("ji,kj->ik", L_tr, bel.W)
        x_transform = jnp.einsum("ij,j->i", term1, x_sample)
        eps_transform = jnp.einsum("ji,j->i", L_tr, eps_sample)
        samples = x_transform + eps_transform
        return samples + bel.mu

    @staticmethod
    def _get_coef(params, bel, x, fwd_link):
        c, std = jax.jacfwd(fwd_link, has_aux=True)(params, bel, x)
        return c * std

    @partial(jax.vmap, in_axes=(None, 0, None, None, None))
    def _sample_grad_expected_log_prob(self, key, bel, x, y):
        """
        E[∇ logp(y|x,θ)]
        """
        mu_sample = self._sample_lr_params(key, bel)
        grads = self.grad_log_prob(mu_sample, x, y)
        return grads

    @partial(jax.vmap, in_axes=(None, 0, None, None))
    def _sample_cov_coeffs(self, key, x, bel):
        params = self._sample_lr_params(key, bel)
        coef = self._get_coef(params, bel, x, self.fwd_link)
        return coef

    def _fa_approx_step(
        self,
        x: Float[Array, "dim_params"],
        bel: LRVGAState,
        bel_prev: LRVGAState,
    ) -> LRVGAState:
        """
        Factor Analysis (FA) approximation to the low-rank (W)
        and diagonal (Psi) matrices.
        """
        # Load data
        W_prev, Psi_prev = bel_prev.W, bel_prev.Psi
        W, Psi = bel.W, bel.Psi
        
        # Initialise basic transformations
        _, dim_latent = W.shape
        I = jnp.eye(dim_latent)
        Psi_inv = 1 / Psi
        
        # Construct helper matrices
        M = I + jnp.einsum("ij,i,ik->jk", W, Psi_inv, W)
        M_inv = jnp.linalg.inv(M)
        V_beta = jnp.einsum("...i,...j,j,jk->ik", x, x, Psi_inv, W)
        V_alpha = (
            jnp.einsum("ij,kj,k,kl->il", W_prev, W_prev, Psi_inv, W) +
            jnp.einsum("i,i,ij->ij", Psi_prev, Psi_inv, W)
        )
        V = self.beta * V_beta + self.alpha * V_alpha
        # Value_update
        # (return transpose of W_solve -- avoid extra transpose op)
        W_solve = I + jnp.einsum("ij,kj,k,kl->li", M_inv, W, Psi_inv, V)
        W = jnp.linalg.solve(W_solve, V.T).T
        Psi = (
            self.beta * jnp.einsum("...i,...i->i", x, x) +
            self.alpha * jnp.einsum("ij,ij->i", W_prev, W_prev) + 
            self.alpha * Psi_prev -
            jnp.einsum("ij,jk,ik->i", W, M_inv, V)
        )
        
        new_bel = bel.replace(
            mu=bel.mu,
            W=W,
            Psi=Psi
        )
        return new_bel

    def _mu_update(
        self,
        key,
        x: Float[Array, "dim_obs"],
        y: float,
        bel_prev: LRVGAState,
        bel: LRVGAState,
    ) -> Float[Array, "dim_obs"]:
        """
        Obtain gain matrix-vector multiplication for the mean update.

        TODO: Optimise for lower compilation time:
            1. Refactor sample_predictions
            2. Refactor sample_grad_expected_log_prob
        TODO: Rewrite the V term using the Woodbury matrix identity
        """
        W = bel.W
        Psi_inv = 1 / bel.Psi
        dim_full, _ = W.shape
        I = jnp.eye(dim_full)

        keys_grad = jax.random.split(key, self.n_samples)

        V = W @ W.T + bel.Psi * I
        exp_grads_log_prob = self._sample_grad_expected_log_prob(keys_grad, bel_prev, x, y).mean(axis=0)
        gain = jnp.linalg.solve(V, exp_grads_log_prob)
        return gain
        
    def _sample_half_fisher(self, key, x, bel):
        """
        Estimate X such that
            X X^T ~ E_{q(θ)}[E_{y}[∇^2 log p(y|x,θ)]]
        """
        keys = jax.random.split(key, self.n_samples)
        coeffs = self._sample_cov_coeffs(keys, x, bel) / jnp.sqrt(self.n_samples)
        # XXtr = jnp.einsum("nji,njk->ik", coeffs, coeffs) / num_samples
        return coeffs

    def _step_lrvga(self, bel, key, x, y):
        """
        Iterated RVGA (§4.2.1). We omit the second iteration of the covariance matrix
        """
        key_fisher, key_est, key_mu_final = jax.random.split(key, 3)

        X = self._sample_half_fisher(key_fisher, x, bel)
        def fa_partial(_, new_bel):
            new_bel = self._fa_approx_step(X, new_bel, bel)
            return new_bel

        # Algorithm 1 in §3.2 of L-RVGA suggests that 1 to 3 loops may be enough in
        # the inner (fa-update) loop (See comments in Algorithm 1)

        # Estimate hat{P} (Eq 36 - 1)
        bel_update = jax.lax.fori_loop(0, self.n_inner_fa, fa_partial, bel)
        # First mu update (Eq 36 - 2)
        mu_add = self._mu_update(key_est, x, y, bel, bel_update)
        mu_new = bel.mu + mu_add
        bel_update = bel_update.replace(mu=mu_new)
        # Second mu update (Eq 36 - 4)
        # we use the updated bel to estimate the gradient
        mu_add = self._mu_update(key_mu_final, x, y, bel_update, bel_update)
        mu_new = bel.mu + mu_add
        bel_update = bel_update.replace(mu=mu_new, step=bel_update.step + 1)
        return bel_update

    def init_bel(self):
        raise NotImplementedError

    def predict_obs(self, bel, X):
        yhat = self.fwd_link(bel.mean, bel, X)[0]
        return yhat

    def predict_state(self, bel):
        """
        L-RVGA doesn't have a closed-form prediction for the state
        (check)
        """
        return bel

    def update_state(self, bel, Xt, yt):
        key = jax.random.fold_in(bel.key, bel.step)

        def _step(i, bel):
            key_i = jax.random.fold_in(key, i)
            bel = self._step_lrvga(bel, key_i, Xt, yt)
            return bel
        bel = jax.lax.fori_loop(0, self.n_inner, _step, bel)
        return bel
