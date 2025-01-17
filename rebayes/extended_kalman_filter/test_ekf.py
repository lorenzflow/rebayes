#pytest test_ekf.py  -rP
# Test inference for Bayesian linear regression with static parameters

import jax.numpy as jnp
from tensorflow_probability.substrates.jax.distributions import MultivariateNormalFullCovariance as MVN
import chex

from dynamax.linear_gaussian_ssm import LinearGaussianSSM

from rebayes.utils.utils import get_mlp_flattened_params
from rebayes.extended_kalman_filter.ekf import EKFParams, RebayesEKF


def allclose(u, v):
    return jnp.allclose(u, v, atol=1e-3)


def make_linreg_data():
    n_obs = 21
    x = jnp.linspace(0, 20, n_obs)
    X = x[:, None] # reshape to (T,1)
    y = jnp.array(
        [2.486, -0.303, -4.053, -4.336, -6.174, -5.604, -3.507, -2.326, -4.638, -0.233, -1.986, 1.028, -2.264,
        -0.451, 1.167, 6.652, 4.145, 5.268, 6.34, 9.626, 14.784])
    Y = y[:, None] # reshape to (T,1)
    return X, Y


def make_linreg_prior():
    obs_var = 0.1
    mu0 = jnp.zeros(2)
    Sigma0 = jnp.eye(2) * 1
    return (obs_var, mu0, Sigma0)


def batch_bayes(X,Y):
    N = X.shape[0]
    X1 = jnp.column_stack((jnp.ones(N), X))  # Include column of 1s
    y = Y[:,0] # extract column vector
    (obs_var, mu0, Sigma0) = make_linreg_prior()
    posterior_prec = jnp.linalg.inv(Sigma0) + X1.T @ X1 / obs_var
    cov_batch = jnp.linalg.inv(posterior_prec)
    b = jnp.linalg.inv(Sigma0) @ mu0 + X1.T @ y / obs_var
    mu_batch = jnp.linalg.solve(posterior_prec, b)
    return mu_batch, cov_batch


def run_kalman(X, Y):
    N = X.shape[0]
    X1 = jnp.column_stack((jnp.ones(N), X))  # Include column of 1s
    (obs_var, mu0, Sigma0) = make_linreg_prior()
    nfeatures = X1.shape[1]
    # we use H=X1 since z=(b, w), so z'u = (b w)' (1 x)
    lgssm = LinearGaussianSSM(state_dim = nfeatures, emission_dim = 1, input_dim = 0)
    F = jnp.eye(nfeatures) # dynamics = I
    Q = jnp.zeros((nfeatures, nfeatures))  # No parameter drift.
    R = jnp.ones((1, 1)) * obs_var

    params, _ = lgssm.initialize(
        initial_mean=mu0,
        initial_covariance=Sigma0,
        dynamics_weights=F,
        dynamics_covariance=Q,
        emission_weights=X1[:, None, :], # (t, 1, D) where D = num input features
        emission_covariance=R,
        )
    lgssm_posterior = lgssm.filter(params, Y) 
    return lgssm_posterior


def test_kalman():
    X, Y = make_linreg_data()
    lgssm_posterior = run_kalman(X, Y)
    mu_kf = lgssm_posterior.filtered_means[-1]
    cov_kf = lgssm_posterior.filtered_covariances[-1]
    mu_batch, cov_batch = batch_bayes(X,Y)
    assert allclose(mu_batch, mu_kf)
    assert allclose(cov_batch, cov_kf)


def make_linreg_rebayes_params(nfeatures):
    (obs_var, mu0, Sigma0) = make_linreg_prior()
    # we pass in X not X1 since DNN has a bias term 
    
    # Define Linear Regression as MLP with no hidden layers
    input_dim, hidden_dims, output_dim = nfeatures, [], 1
    model_dims = [input_dim, *hidden_dims, output_dim]
    _, flat_params, _, apply_fn = get_mlp_flattened_params(model_dims)
    nparams = len(flat_params)
    
    params = EKFParams(
        initial_mean=mu0,
        initial_covariance=1.0,
        dynamics_weights_or_function = 1.0,
        dynamics_covariance = 0.0,
        emission_mean_function = lambda w, x: apply_fn(w, x),
        emission_cov_function = lambda w, x: obs_var
    )

    return params


def test_rebayes_loop():
    (X, Y) = make_linreg_data()
    N, D = X.shape
    params  = make_linreg_rebayes_params(D)
    estimator = RebayesEKF(params, method='fcekf')

    lgssm_posterior = run_kalman(X, Y)
    mu_kf = lgssm_posterior.filtered_means
    cov_kf = lgssm_posterior.filtered_covariances
    ll_kf = lgssm_posterior.marginal_loglik
    def callback(bel, pred_obs, t, u, y, bel_pred):
        m = estimator.predict_obs(bel_pred, u)
        assert allclose(pred_obs, m)
        P = estimator.predict_obs_cov(bel_pred, u)
        #m, P = pred_obs.mean, pred_obs.cov
        ll = MVN(m, P).log_prob(jnp.atleast_1d(y))
        assert allclose(bel.mean, mu_kf[t])
        assert allclose(bel.cov, cov_kf[t])
        return  ll

    bel = estimator.init_bel()
    T = X.shape[0]
    ll = 0
    for t in range(T):
        pred_obs = estimator.predict_obs(bel, X[t])
        bel_pred = estimator.predict_state(bel)
        bel = estimator.update_state(bel_pred, X[t], Y[t]) 
        ll += callback(bel, pred_obs, t, X[t], Y[t], bel_pred)  
    assert jnp.allclose(ll, ll_kf, atol=1e-1)


def test_rebayes_scan():
    (X, Y) = make_linreg_data()
    N, D = X.shape
    params  = make_linreg_rebayes_params(D)
    estimator = RebayesEKF(params, method='fcekf')

    lgssm_posterior = run_kalman(X, Y)
    mu_kf = lgssm_posterior.filtered_means
    cov_kf = lgssm_posterior.filtered_covariances
    ll_kf = lgssm_posterior.marginal_loglik

    def callback(bel, pred_obs, t, u, y, bel_pred):
        m = estimator.predict_obs(bel_pred, u)
        P = estimator.predict_obs_cov(bel_pred, u)
        #m, P = pred_obs.mean, pred_obs.cov
        ll = MVN(m, P).log_prob(jnp.atleast_1d(y))
        return ll

    final_bel, lls = estimator.scan(X, Y,  callback)
    T = mu_kf.shape[0]
    assert allclose(final_bel.mean, mu_kf[T-1])
    assert allclose(final_bel.cov, cov_kf[T-1])
    print(lls)
    ll = jnp.sum(lls)
    assert jnp.allclose(ll, ll_kf, atol=1e-1)

if __name__ == "__main__":
    test_kalman()
    test_rebayes_loop()
    test_rebayes_scan()
