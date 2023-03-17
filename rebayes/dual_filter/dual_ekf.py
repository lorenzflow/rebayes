

from jaxtyping import Float, Array
from typing import Callable, NamedTuple, Union, Tuple, Any, Literal
from functools import partial
import chex
import optax
import jax
import jax.numpy as jnp
import jax.random as jr
from jax import lax, jacfwd, vmap, grad, jit, jacrev
from jax.tree_util import tree_map, tree_reduce
from jax.flatten_util import ravel_pytree

import flax
import flax.linen as nn
from tensorflow_probability.substrates.jax.distributions import MultivariateNormalFullCovariance as MVN

import matplotlib.pyplot as plt
import matplotlib.cm as cm
from dataclasses import dataclass
from collections import namedtuple
from itertools import cycle

from rebayes.dual_filter.dual_estimator import DualBayesParams, ObsModel, GaussBel, RebayesEstimator
from rebayes.extended_kalman_filter.ekf import _full_covariance_dynamics_predict, _diagonal_dynamics_predict
from rebayes.extended_kalman_filter.ekf import _full_covariance_condition_on,  _variational_diagonal_ekf_condition_on,  _fully_decoupled_ekf_condition_on

_jacrev_2d = lambda f, x: jnp.atleast_2d(jacrev(f)(x))

EKFMethods = Literal["fcekf", "vdekf", "fdekf"]

EKFParams = namedtuple("EKFParams", ["method", "obs_noise_var_lr"])

def make_dual_ekf_estimator(params: DualBayesParams, obs: ObsModel, ekf_params: EKFParams):

    def init():
        D = params.mu0.shape[0]
        if ekf_params.method == 'fcekf':
            cov = 1/params.eta0 * jnp.eye(D)
        else: # store diagonal cov as a vector
            cov = 1/params.eta0 * jnp.ones(D)
        bel =  GaussBel(mean=params.mu0, cov=cov)
        return params, bel
    
    def predict_state(params, bel):
        m, P = bel.mean, bel.cov
        if ekf_params.method == 'fcekf':
            pred_mean, pred_cov = _full_covariance_dynamics_predict(m, P, params.q, params.gamma, params.alpha)
        else:
            pred_mean, pred_cov = _diagonal_dynamics_predict(m, P, params.q, params.gamma, params.alpha)
        return GaussBel(mean=pred_mean, cov=pred_cov)
    
    def update_state(params, bel, X, Y):
        m, P = bel.mean, bel.cov
        if ekf_params.method == 'fcekf':
            update_fn = _full_covariance_condition_on
        elif ekf_params.method == 'vdekf':
            update_fn = _variational_diagonal_ekf_condition_on
        elif ekf_params.method == 'fdekf':
            update_fn = _fully_decoupled_ekf_condition_on
        adapt_obs_noise_var = (ekf_params.obs_noise_var_lr > 0)
        mu, Sigma = update_fn(m, P, obs.emission_mean_function,
                            obs.emission_cov_function, X, Y, num_iter=1,
                            adaptive_variance=adapt_obs_noise_var,
                            obs_noise_var=params.obs_noise_var)
        return GaussBel(mean=mu, cov=Sigma)
    
    def predict_obs(params, bel, X):
        prior_mean, prior_cov = bel.mean, bel.cov
        m_Y = lambda z: obs.emission_mean_function(z, X)
        y_pred = jnp.atleast_1d(m_Y(prior_mean))
        return y_pred

    def predict_obs_cov(params, bel, X):
        prior_mean, prior_cov = bel.mean, bel.cov
        m_Y = lambda z: obs.emission_mean_function(z, X)
        H =  _jacrev_2d(m_Y, prior_mean)
        y_pred = jnp.atleast_1d(m_Y(prior_mean))
        adapt_obs_noise_var = (ekf_params.obs_noise_var_lr > 0)
        if adapt_obs_noise_var:
            R = jnp.eye(y_pred.shape[0]) * params.obs_noise_var
        else:
            R = jnp.atleast_2d(obs.emission_cov_function(prior_mean, X))
        if ekf_params.method == 'fcekf':
            V_epi = H @ prior_cov @ H.T
        else:
            V_epi = (prior_cov * H) @ H.T
        Sigma_obs = V_epi + R
        return Sigma_obs
    
    def update_params(params, t, X, y, yhat):
        adapt_obs_noise_var = (ekf_params.obs_noise_var_lr > 0)
        if adapt_obs_noise_var:
            yhat = jnp.atleast_1d(yhat)
            r_old = params.obs_noise_var
            nobs = params.nobs
            sqerr = ((yhat - y).T @ (yhat - y)).squeeze() / yhat.shape[0]
            nobs += 1
            #obs_noise_var = jnp.max(jnp.array([1e-6, obs_noise_var + 1/nobs * (sqerr - obs_noise_var)]))
            lr = ekf_params.obs_noise_var_lr/nobs # decay learning rate over time
            r = (1-lr)*r_old + lr*sqerr
            obs_noise_var = jnp.max(jnp.array([1e-6, r]))
            params = params.replace(nobs = nobs, obs_noise_var = obs_noise_var)
        return params
    
    return RebayesEstimator(init, predict_state, update_state, predict_obs, predict_obs_cov, update_params)