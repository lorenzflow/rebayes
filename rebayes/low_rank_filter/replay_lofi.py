from functools import partial
from typing import Any, Callable, NamedTuple, Tuple, Union

import chex
from jax import jit, lax, vmap, tree_util
import jax.numpy as jnp
from jax_tqdm import scan_tqdm
from jaxtyping import Array, Float
import tensorflow_probability.substrates.jax as tfp

from rebayes.base import (
    CovMat,
    EmissionDistFn,
    FnStateAndInputToEmission,
    Rebayes,
)
from rebayes.low_rank_filter.lofi_core import (
    _jacrev_2d,
    _lofi_spherical_cov_inflate,
    _lofi_spherical_cov_predict,
    _lofi_spherical_cov_svd_free_predict,
    _lofi_estimate_noise,
    _lofi_spherical_cov_condition_on,
    _lofi_spherical_cov_svd_free_condition_on,
    _lofi_orth_condition_on,
    _lofi_diagonal_cov_inflate,
    _lofi_diagonal_cov_predict,
    _replay_lofi_diagonal_cov_condition_on,
    _replay_lofi_diagonal_cov_svd_free_condition_on,
)
from rebayes.utils.sampling import sample_dlr


# Common Classes ---------------------------------------------------------------

tfd = tfp.distributions
MVN = tfd.MultivariateNormalTriL


INFLATION_METHODS = [
    'bayesian',
    'simple',
    'hybrid',
]


FnStateStateInputToEmission = Callable[ [Float[Array, "state_dim"], Float[Array, "state_dim"], Float[Array, "input_dim"] ], Float[Array, "emission_dim"]]


def _initialize_buffer(item, buffer_size):
    ndim = 0
    if isinstance(item, jnp.ndarray):
        ndim = item.ndim
    pad_arg = [[0, 0]] * ndim
    buffer = jnp.pad(
        jnp.expand_dims(item, 0), 
        [[buffer_size-1, 0], *pad_arg], 
        'constant', 
        constant_values=0
    )

    return buffer


@chex.dataclass
class ReplayLoFiBel:
    buffer_X: chex.Array
    buffer_y: chex.Array
    buffer_pp_mean: chex.Array
    buffer_mean: chex.Array
    buffer_basis: chex.Array
    buffer_svs: chex.Array
    buffer_eta: chex.Array
    buffer_Ups: chex.Array
    buffer_obs_noise_var: chex.Array
    
    pp_mean: chex.Array
    mean: chex.Array
    mean_lin: chex.Array # Linearization point
    basis: chex.Array
    svs: chex.Array
    eta: float
    gamma: float
    q: float

    Ups: CovMat = None
    nobs: int = 0
    obs_noise_var: float = 1.0
    
    def _update_buffer(self, buffer, item):
        buffer_new = jnp.concatenate([buffer[1:], jnp.expand_dims(item, 0)], axis=0)

        return buffer_new

    def apply_io_buffers(self, X, y):
        buffer_X = self._update_buffer(self.buffer_X, X)
        buffer_y = self._update_buffer(self.buffer_y, y)

        return self.replace(
            buffer_X=buffer_X,
            buffer_y=buffer_y,
        )
    
    def apply_param_buffers(self):
        m0, m , U, Lambda, eta, Ups, obs_noise_var = \
            self.pp_mean, self.mean, self.basis, self.svs, self.eta, self.Ups, self.obs_noise_var
        buffer_pp_mean = self._update_buffer(self.buffer_pp_mean, m0)
        buffer_mean = self._update_buffer(self.buffer_mean, m)
        buffer_basis = self._update_buffer(self.buffer_basis, U)
        buffer_svs = self._update_buffer(self.buffer_svs, Lambda)
        buffer_eta = self._update_buffer(self.buffer_eta, eta)
        buffer_Ups = self._update_buffer(self.buffer_Ups, Ups)
        buffer_obs_noise_var = self._update_buffer(self.buffer_obs_noise_var, obs_noise_var)

        return self.replace(
            buffer_pp_mean=buffer_pp_mean,
            buffer_mean=buffer_mean,
            buffer_basis=buffer_basis,
            buffer_svs=buffer_svs,
            buffer_eta=buffer_eta,
            buffer_Ups=buffer_Ups,
            buffer_obs_noise_var=buffer_obs_noise_var,
        )
    

class ReplayLoFiParams(NamedTuple):
    buffer_size: int
    dim_input: int
    dim_output: int
    initial_mean: Float[Array, "state_dim"]
    initial_covariance: CovMat
    dynamics_weights: CovMat
    dynamics_covariance: CovMat
    emission_mean_function: FnStateAndInputToEmission
    emission_cov_function: FnStateStateInputToEmission
    emission_dist: EmissionDistFn = \
        lambda mean, cov: MVN(loc=mean, scale_tril=jnp.linalg.cholesky(cov))
    adaptive_emission_cov: bool=False
    dynamics_covariance_inflation_factor: float=0.0
    memory_size: int = 10
    steady_state: bool = False
    inflation: str = 'bayesian'
    use_svd: bool = True
    n_inner: int = 1


class RebayesReplayLoFi(Rebayes):
    def __init__(
        self,
        params: ReplayLoFiParams,
    ):
        super().__init__(params)
        
        # Check inflation type
        if params.inflation not in INFLATION_METHODS:
            raise ValueError(f"Unknown inflation method: {params.inflation}.")

    def init_bel(self):
        pp_mean = self.params.initial_mean # Predictive prior mean
        init_mean = self.params.initial_mean # Initial mean
        memory_size = self.params.memory_size
        init_basis = jnp.zeros((len(init_mean), memory_size)) # Initial basis
        init_svs = jnp.zeros(memory_size) # Initial singular values
        init_eta = 1 / self.params.initial_covariance # Initial precision
        gamma = self.params.dynamics_weights # Dynamics weights
        q = self.params.dynamics_covariance # Dynamics covariance
        if self.params.steady_state: # Steady-state constraint
            q = self.steady_state_constraint(init_eta, gamma)
        init_Ups = jnp.ones((len(init_mean), 1)) * init_eta
        
        # Set up buffers
        L = self.params.buffer_size
        D, C = self.params.dim_input, self.params.dim_output
        if isinstance(D, int):
            buffer_X = jnp.zeros((L, D))
        else:
            buffer_X = jnp.zeros((L, *D))
        buffer_Y = jnp.zeros((L, C))
        init_buffer = partial(_initialize_buffer, buffer_size=L)
        buffer_pp_mean, buffer_mean = init_buffer(pp_mean), init_buffer(init_mean)
        buffer_basis, buffer_svs = init_buffer(init_basis), init_buffer(init_svs)
        buffer_eta, buffer_Ups = init_buffer(init_eta), init_buffer(init_Ups)
        obs_noise_var = 1.0
        buffer_obs_noise_var = init_buffer(obs_noise_var)

        return ReplayLoFiBel(
            buffer_X = buffer_X,
            buffer_y = buffer_Y,
            buffer_pp_mean = buffer_pp_mean,
            buffer_mean = buffer_mean,
            buffer_basis = buffer_basis,
            buffer_svs = buffer_svs,
            buffer_eta = buffer_eta,
            buffer_Ups = buffer_Ups,
            buffer_obs_noise_var = buffer_obs_noise_var,
            pp_mean = pp_mean,
            mean = init_mean,
            mean_lin = init_mean,
            basis = init_basis,
            svs = init_svs,
            eta = init_eta,
            gamma = gamma,
            q = q,
            Ups = init_Ups,
            obs_noise_var = obs_noise_var,
        )

    @staticmethod
    def steady_state_constraint(
        eta: float,
        gamma: float,
    ) -> float:
        """Return dynamics covariance according to the steady-state constraint."""
        q = (1 - gamma**2) / eta

        return q

    @partial(jit, static_argnums=(0,))
    def predict_obs(
        self,
        bel: ReplayLoFiBel,
        x: Float[Array, "input_dim"],
    ) -> Union[Float[Array, "output_dim"], Any]:
        m = bel.mean
        m_Y = lambda z: self.params.emission_mean_function(z, x)
        y_pred = jnp.atleast_1d(m_Y(m))

        return y_pred
    
    @partial(jit, static_argnums=(0,))
    def predict_state(
        bel: ReplayLoFiBel,
    ) -> ReplayLoFiBel:
        
        raise NotImplementedError
    
    @partial(jit, static_argnums=(0,))
    def _update_state(
        bel: ReplayLoFiBel,
        x: Float[Array, "input_dim"],
        y: Float[Array, "output_dim"],
    ) -> ReplayLoFiBel:
        
        raise NotImplementedError
    
    @partial(jit, static_argnums=(0,))
    def replay_update_step(
        self,
        bel: ReplayLoFiBel,
    ) -> ReplayLoFiBel:
        X, y = bel.buffer_X, bel.buffer_y
        init_nobs = bel.nobs
        num_timesteps = X.shape[0]
        def _step(t, bel):
            bel_pred = self.predict_state(bel)
            # bel_pred = bel_pred.replace(mean_lin = bel_pred.mean)
            bel = self._update_state(bel_pred, X[t], y[t])
            
            return bel
        bel = lax.fori_loop(0, num_timesteps, _step, bel)
        bel = bel.replace(nobs=init_nobs)
        
        return bel
    
    @partial(jit, static_argnums=(0,))
    def update_state_without_replay(
        self,
        bel: ReplayLoFiBel,
        x: Float[Array, "input_dim"],
        y: Float[Array, "output_dim"],
    ) -> ReplayLoFiBel:
        bel = bel.apply_io_buffers(x, y)
        bel = bel.replace(mean_lin = bel.mean)
        bel = self._update_state(bel, x, y)
        bel = bel.apply_param_buffers()
        
        return bel
    
    @partial(jit, static_argnums=(0,))
    def update_state_with_replay(
        self,
        bel: ReplayLoFiBel,
        x: Float[Array, "input_dim"],
        y: Float[Array, "output_dim"],
    ) -> ReplayLoFiBel:
        bel = bel.apply_io_buffers(x, y)
        bel = bel.replace(
            pp_mean = bel.buffer_pp_mean[0],
            mean = bel.buffer_mean[0],
            mean_lin = bel.mean,
            basis = bel.buffer_basis[0],
            svs = bel.buffer_svs[0],
            eta = bel.buffer_eta[0],
            Ups = bel.buffer_Ups[0],
            obs_noise_var = bel.buffer_obs_noise_var[0],
        )
        
        def partial_step(_, bel):
            bel = self.replay_update_step(bel)
            
            return bel
        bel = lax.fori_loop(0, self.params.n_inner, partial_step, bel)
        bel = bel.replace(nobs=bel.nobs+1)
        bel = bel.apply_param_buffers()
        
        return bel
    
    def scan(
        self,
        X: Float[Array, "ntime input_dim"],
        Y: Float[Array, "ntime emission_dim"],
        callback=None,
        bel=None,
        progress_bar=False,
        debug=False,
        Xinit=None,
        Yinit=None,
        **kwargs
    ) -> Tuple[ReplayLoFiBel, Any]:
        """Apply filtering to entire sequence of data. Return final belief state and outputs from callback."""
        num_timesteps = X.shape[0]
        def step(bel, t, update_fn):
            bel_pred = self.predict_state(bel)
            pred_obs = self.predict_obs(bel, X[t])
            bel = update_fn(bel_pred, X[t], Y[t])
            out = None
            if callback is not None:
                out = callback(bel, pred_obs, t, X[t], Y[t], bel_pred, **kwargs)
            
            return bel, out
        warmup_steps = min(self.params.buffer_size-1, num_timesteps)
        step_without_replay = partial(step, update_fn=self.update_state_without_replay)
        step_with_replay = partial(step, update_fn=self.update_state_with_replay)
        carry = bel
        if bel is None:
            if Xinit is not None:
                carry = self.init_bel(Xinit, Yinit)
            else:
                carry = self.init_bel()
        if progress_bar:
            step_with_replay = scan_tqdm(num_timesteps)(step_with_replay)
        if debug:
            outputs = []
            for t in range(warmup_steps):
                carry, out = step_without_replay(carry, t)
                outputs.append(out)
            for t in range(warmup_steps, num_timesteps):
                carry, out = step_with_replay(carry, t)
                outputs.append(out)
            bel = carry
            outputs = jnp.stack(outputs)
        else:
            bel, outputs1 = lax.scan(step_without_replay, carry, jnp.arange(warmup_steps))
            bel, outputs2 = lax.scan(step_with_replay, bel, jnp.arange(warmup_steps, num_timesteps))
            if outputs1 is None:
                outputs = outputs2
            elif outputs2 is None:
                outputs = outputs1
            else:
                outputs = tree_util.tree_map(
                    lambda l1, l2: jnp.concatenate([l1, l2]), outputs1, outputs2
                )
        return bel, outputs
    

class RebayesReplayLoFiDiagonal(RebayesReplayLoFi):
    def __init__(
        self,
        params: ReplayLoFiParams,
    ):
        super().__init__(params)

    @partial(jit, static_argnums=(0,))
    def predict_state(
        self,
        bel: ReplayLoFiBel,
    ) -> ReplayLoFiBel:
        m0, m, U, Lambda, eta, gamma, q, Ups = \
            bel.pp_mean, bel.mean, bel.basis, bel.svs, bel.eta, bel.gamma, bel.q, bel.Ups
        alpha = self.params.dynamics_covariance_inflation_factor
        inflation = self.params.inflation

        # Inflate posterior covariance.
        m_infl, U_infl, Lambda_infl, Ups_infl = \
            _lofi_diagonal_cov_inflate(m0, m, U, Lambda, eta, Ups, alpha, inflation)

        # Predict dynamics.
        pp_mean_pred, m_pred, U_pred, Lambda_pred, eta_pred, Ups_pred = \
            _lofi_diagonal_cov_predict(m0, m_infl, U_infl, Lambda_infl, gamma, q, eta, Ups_infl)

        bel_pred = bel.replace(
            pp_mean = pp_mean_pred,
            mean = m_pred,
            basis = U_pred,
            svs = Lambda_pred,
            eta = eta_pred,
            Ups = Ups_pred,
        )

        return bel_pred

    @partial(jit, static_argnums=(0,))
    def predict_obs_cov(
        self,
        bel: ReplayLoFiBel,
        x: Float[Array, "input_dim"]
    ) -> Union[Float[Array, "output_dim output_dim"], Any]:
        m, U, Lambda, obs_noise_var, Ups = \
            bel.mean, bel.basis, bel.svs, bel.eta, bel.obs_noise_var
        m_Y = lambda z: self.params.emission_mean_function(z, x)
        Cov_Y = lambda z: self.params.emission_cov_function(z, z, x)

        C = jnp.atleast_1d(m_Y(m)).shape[0]
        H = _jacrev_2d(m_Y, m)
        if self.params.adaptive_emission_cov:
            R = jnp.eye(C) * obs_noise_var
        else:
            R = jnp.atleast_2d(Cov_Y(m))

        P, L = U.shape
        W = U * Lambda
        G = jnp.linalg.pinv(jnp.eye(L) +  W.T @ (W/Ups))
        HW = H/Ups @ W
        V_epi = H @ H.T/Ups - (HW @ G) @ (HW).T
        Sigma_obs = V_epi + R

        return Sigma_obs

    @partial(jit, static_argnums=(0,))
    def _update_state(
        self,
        bel: ReplayLoFiBel,
        x: Float[Array, "input_dim"],
        y: Float[Array, "output_dim"],
    ) -> ReplayLoFiBel:
        m, m_lin, U, Lambda, Ups, nobs, obs_noise_var = \
            bel.mean, bel.mean_lin, bel.basis, bel.svs, bel.Ups, bel.nobs, bel.obs_noise_var

        # Condition on observation.
        update_fn = _replay_lofi_diagonal_cov_condition_on if self.params.use_svd \
            else _replay_lofi_diagonal_cov_svd_free_condition_on

        m_cond, U_cond, Lambda_cond, Ups_cond = \
            update_fn(m, m_lin, U, Lambda, Ups, self.params.emission_mean_function,
                      self.params.emission_cov_function, x, y,
                      self.params.adaptive_emission_cov, obs_noise_var)

        # Estimate emission covariance.
        nobs_est, obs_noise_var_est = \
            _lofi_estimate_noise(m_cond, self.params.emission_mean_function,
                                 x, y, nobs, obs_noise_var,
                                 self.params.adaptive_emission_cov)
        bel_cond = bel.replace(
            mean = m_cond,
            basis = U_cond,
            svs = Lambda_cond,
            Ups = Ups_cond,
            nobs = nobs_est,
            obs_noise_var = obs_noise_var_est,
        )

        return bel_cond

    @partial(jit, static_argnums=(0,4))
    def pred_obs_mc(self, key, bel, x, shape=None):
        """
        Sample observations from the posterior predictive distribution.
        """
        shape = shape or (1,)
        # Belief posterior predictive.
        bel = self.predict_state(bel)
        params_sample = sample_dlr(key, bel.basis, bel.Ups.ravel(), shape) + bel.mean
        yhat_samples = vmap(self.params.emission_mean_function, (0, None))(params_sample, x)
        return yhat_samples

    @partial(jit, static_argnames=("self", "n_samples"))
    def nlpd_mc(self, key, bel, x, y, n_samples=30):
        """
        Compute the negative log predictive density (nlpd) as
        a Monte Carlo estimate.
        llfn: log likelihood function
            Takes mean, x, y
        """
        x = jnp.atleast_2d(x)
        y = jnp.atleast_2d(y)
        shape = (n_samples,)
        bel = self.predict_state(bel)
        params_sample = sample_dlr(key, bel.basis, bel.Ups.ravel(), shape) + bel.mean
        scale = jnp.sqrt(self.params.emission_cov_function(0.0, 0.0, 0.0))
        def llfn(params, x, y):
            y = y.ravel()
            mean = self.params.emission_mean_function(params, x).ravel()
            log_likelihood = self.params.emission_dist(mean, scale).log_prob(y)
            return log_likelihood.sum()

        # Compute vectorised nlpd
        vnlpd = vmap(llfn, (0, None, None))
        vnlpd = vmap(vnlpd, (None, 0, 0))
        nlpd_vals = -vnlpd(params_sample, x, y).squeeze()

        return nlpd_vals.mean(axis=-1)