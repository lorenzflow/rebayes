from typing import Tuple, Callable

from jax import jacrev
from jax.lax import scan
import jax.numpy as jnp
import jax.random as jr
from jaxtyping import Float, Array


# Helper functions -------------------------------------------------------------

_jacrev_2d = lambda f, x: jnp.atleast_2d(jacrev(f)(x))
_normalize = lambda v: jnp.where(v.any(), v / jnp.linalg.norm(v), jnp.zeros(shape=v.shape))


def _invert_2x2_block_matrix(
    M: Float[Array, "m n"],
    lr_block_dim: int
) -> Float[Array, "m n"]:
    """Invert a 2x2 block matrix. The matrix is assumed to be of the form:
    [[A, b],
    [b.T, c]]
    where A is a diagonal matrix.

    Args:
        M (2, 2): 2x2 block matrix.
        lr_block_dim (int): Dimension of the lower right block.
        
    Returns:
        (2, 2): Inverse of the 2x2 block matrix.
    """
    m, n = M.shape
    A = M[:m-lr_block_dim, :n-lr_block_dim]
    B = M[:m-lr_block_dim, n-lr_block_dim:]
    D = M[m-lr_block_dim:, n-lr_block_dim:]
    a = 1/jnp.diag(A)
    K_inv = jnp.linalg.inv(D - (a*B.T) @ B)

    B_inv = - (a * B.T).T @ K_inv
    A_inv = jnp.diag(a) + (a * B.T).T @ K_inv @ (a * B.T)
    C_inv = -K_inv @ (a * B.T)
    D_inv = K_inv

    return jnp.block([[A_inv, B_inv], [C_inv, D_inv]])


# Common inference functions ---------------------------------------------------

def _lofi_estimate_noise(
    m: Float[Array, "state_dim"],
    y_cond_mean: Callable,
    x: Float[Array, "input_dim"],
    y: Float[Array, "obs_dim"],
    nobs: int,
    obs_noise_var: float,
    adaptive_variance: bool = False
) -> Tuple[int, float]:
    """Estimate observation noise based on empirical residual errors.

    Args:
        m (D_hid,): Prior mean.
        y_cond_mean (Callable): Conditional emission mean function.
        x (D_in,): Control input.
        y (D_obs,): Emission.
        nobs (int): Number of observations seen so far.
        obs_noise_var (float): Current estimate of observation noise.
        adaptive_variance (bool): Whether to use adaptive variance.

    Returns:
        nobs_est (int): Updated number of observations seen so far.
        obs_noise_var_est (float): Updated estimate of observation noise.
    """
    if not adaptive_variance:
        return 0, 0.0

    m_Y = lambda w: y_cond_mean(w, x)
    yhat = jnp.atleast_1d(m_Y(m))
    
    sqerr = ((yhat - y).T @ (yhat - y)).squeeze() / yhat.shape[0]
    nobs_est = nobs + 1
    obs_noise_var_est = jnp.max(jnp.array([1e-6, obs_noise_var + 1/nobs_est * (sqerr - obs_noise_var)]))

    return nobs_est, obs_noise_var_est


# Spherical LOFI ---------------------------------------------------------------

def _lofi_spherical_cov_inflate(
    m0: Float[Array, "state_dim"],
    m: Float[Array, "state_dim"],
    U: Float[Array, "state_dim memory_size"],
    Lambda: Float[Array, "memory_size"],
    eta: float,
    alpha: float,
    inflation: str = "bayesian"
):
    """Inflate the spherical posterior covariance matrix.

    Args:
        m0 (D_hid,): Prior predictive mean.
        m (D_hid,): Prior mean.
        U (D_hid, D_mem,): Prior basis.
        Lambda (D_mem,): Prior signular values.
        eta (float): Prior precision.
        alpha (float): Covariance inflation factor.
        inflation (str, optional): Type of inflation. Defaults to 'bayesian'.

    Returns:
        m_infl (D_hid,): Post-inflation mean.
        U_infl (D_hid, D_mem,): Post-inflation basis.
        Lambda_infl (D_mem,): Post-inflation singular values.
        eta_infl (float): Post-inflation precision.
    """    
    Lambda_infl = Lambda / jnp.sqrt(1+alpha)
    U_infl = U
    W_infl = U_infl * Lambda_infl
    
    if inflation == 'bayesian':
        eta_infl = eta
        G = jnp.linalg.pinv(jnp.eye(W_infl.shape[1]) +  (W_infl.T @ (W_infl/eta_infl)))
        e = (m0 - m)
        K = e - ((W_infl/eta_infl) @ G) @ (W_infl.T @ e)
        m_infl = m + alpha/(1+alpha) * K.ravel()
    elif inflation == 'simple':
        eta_infl = eta/(1+alpha)
        m_infl = m
    elif inflation == 'hybrid':
        eta_infl = eta
        m_infl = m
    
    return m_infl, U_infl, Lambda_infl, eta_infl


def _lofi_spherical_cov_predict(
    m0: Float[Array, "state_dim"],
    m: Float[Array, "state_dim"],
    U: Float[Array, "state_dim memory_size"],
    Lambda: Float[Array, "memory_size"],
    gamma: float,
    q: float,
    eta: float,
    steady_state: bool = False
):
    """Predict step of the spherical low-rank filter algorithm.

    Args:
        m0 (D_hid,): Prior predictive mean.
        m (D_hid,): Prior mean.
        U (D_hid, D_mem,): Prior basis.
        Lambda (D_mem,): Prior singluar values.
        gamma (float): Dynamics decay factor.
        q (float): Dynamics noise factor.
        eta (float): Prior precision.
        alpha (float): Covariance inflation factor.
        steady_state (bool): Whether to use steady-state dynamics.

    Returns:
        m0_pred (D_hid,): Predicted predictive mean.
        m_pred (D_hid,): Predicted mean.
        Lambda_pred (D_mem,): Predicted singular values.
        eta_pred (float): Predicted precision.
    """
    # Mean prediction
    m0_pred = gamma*m0
    m_pred = gamma*m

    # Covariance prediction
    U_pred = U
    
    if steady_state:
        eta_pred = eta
        Lambda_pred = jnp.sqrt(
            (gamma**2 * Lambda**2) /
            (1 + q*Lambda**2)
        )
    else:
        eta_pred = eta/(gamma**2 + q*eta)
        Lambda_pred = jnp.sqrt(
            (gamma**2 * Lambda**2) /
            ((gamma**2 + q*eta) * (gamma**2 + q*eta + q*Lambda**2))
        )

    return m0_pred, m_pred, U_pred, Lambda_pred, eta_pred


def _lofi_spherical_cov_condition_on(m, U, Sigma, eta, y_cond_mean, y_cond_cov, x, y, sv_threshold, adaptive_variance=False, obs_noise_var=1.0):
    """Condition step of the low-rank filter with adaptive observation variance.

    Args:
        m (D_hid,): Prior mean.
        U (D_hid, D_mem,): Prior basis.
        Sigma (D_mem,): Prior singular values.
        eta (float): Prior precision. 
        y_cond_mean (Callable): Conditional emission mean function.
        y_cond_cov (Callable): Conditional emission covariance function.
        x (D_in,): Control input.
        y (D_obs,): Emission.
        sv_threshold (float): Threshold for singular values.
        adaptive_variance (bool): Whether to use adaptive variance.

    Returns:
        m_cond (D_hid,): Posterior mean.
        U_cond (D_hid, D_mem,): Posterior basis.
        Sigma_cond (D_mem,): Posterior singular values.
    """
    m_Y = lambda w: y_cond_mean(w, x)
    Cov_Y = lambda w: y_cond_cov(w, x)
    
    yhat = jnp.atleast_1d(m_Y(m))
    if adaptive_variance:
        R = jnp.eye(yhat.shape[0]) * obs_noise_var
    else:
        R = jnp.atleast_2d(Cov_Y(m))
    L = jnp.linalg.cholesky(R)
    A = jnp.linalg.lstsq(L, jnp.eye(L.shape[0]))[0].T
    H = _jacrev_2d(m_Y, m)
    W_tilde = jnp.hstack([Sigma * U, (H.T @ A).reshape(U.shape[0], -1)])

    # Update the U matrix
    u, lamb, _ = jnp.linalg.svd(W_tilde, full_matrices=False)

    D = (lamb**2)/(eta**2 + eta * lamb**2)
    K = (H.T @ A) @ A.T/eta - (D * u) @ (u.T @ ((H.T @ A) @ A.T))

    U_cond = u[:, :U.shape[1]]
    Sigma_cond = lamb[:U.shape[1]]

    m_cond = m + K @ (y - yhat)

    return m_cond, U_cond, Sigma_cond, eta


# Diagonal LOFI ----------------------------------------------------------------

def _lofi_diagonal_cov_inflate(m0, m, U, Sigma, gamma, q, eta, Ups, alpha, inflation='bayesian'):
    """Inflate the diagonal covariance matrix.

    Args:
        Ups (D_hid,): Prior diagonal covariance.
        alpha (float): Covariance inflation factor.

    Returns:
        Ups (D_hid,): Inflated diagonal covariance.
    """
    W = U * Sigma
    eta_pred = eta/(gamma**2 + q*eta)
    
    if inflation == 'bayesian':
        W_pred = W/jnp.sqrt(1+alpha)
        Ups_pred = Ups/(1+alpha) + alpha*eta/(1+alpha)
        G = jnp.linalg.pinv(jnp.eye(W.shape[1]) +  (W_pred.T @ (W_pred/Ups_pred)))
        e = (m0 - m)
        K = 1/Ups_pred.ravel() * (e - (W_pred @ G) @ ((W_pred/Ups_pred).T @ e))
        m_pred = m + alpha*eta/(1+alpha) * K
    elif inflation == 'simple':
        W_pred = W/jnp.sqrt(1+alpha)
        Ups_pred = Ups/(1+alpha)
        m_pred = m
    elif inflation == 'hybrid':
        W_pred = W/jnp.sqrt(1+alpha)
        Ups_pred = Ups/(1+alpha) + alpha*eta/(1+alpha)
        m_pred = m
    U_pred, Sigma_pred, _ = jnp.linalg.svd(W_pred, full_matrices=False)
    
    return m_pred, U_pred, Sigma_pred, Ups_pred, eta_pred


def _lofi_diagonal_cov_predict(m, U, Sigma, gamma, q, Ups, steady_state=False):
    """Predict step of the generalized low-rank filter algorithm.

    Args:
        m0 (D_hid,): Initial mean.
        m (D_hid,): Prior mean.
        U (D_hid, D_mem,): Prior basis.
        Sigma (D_mem,): Prior singluar values.
        gamma (float): Dynamics decay factor.
        q (float): Dynamics noise factor.
        eta (float): Prior precision.
        Ups (D_hid,): Prior diagonal covariance.
        alpha (float): Covariance inflation factor.

    Returns:
        m_pred (D_hid,): Predicted mean.
        U_pred (D_hid, D_mem,): Predicted basis.
        Sigma_pred (D_mem,): Predicted singular values.
        eta_pred (float): Predicted precision.
    """
    # Mean prediction
    W = U * Sigma
    m_pred = gamma*m

    # Covariance prediction
    Ups_pred = 1/(gamma**2/Ups + q)
    C = jnp.linalg.pinv(jnp.eye(W.shape[1]) + q*W.T @ (W*(Ups_pred/Ups)))
    W_pred = gamma*(Ups_pred/Ups)*W @ jnp.linalg.cholesky(C)
    U_pred, Sigma_pred, _ = jnp.linalg.svd(W_pred, full_matrices=False)
    
    return m_pred, U_pred, Sigma_pred, Ups_pred


def _lofi_diagonal_cov_condition_on(m, U, Sigma, Ups, y_cond_mean, y_cond_cov, x, y, sv_threshold, adaptive_variance=False, obs_noise_var=1.0):
    """Condition step of the low-rank filter with adaptive observation variance.

    Args:
        m (D_hid,): Prior mean.
        U (D_hid, D_mem,): Prior basis.
        Sigma (D_mem,): Prior singular values.
        Ups (D_hid): Prior precision. 
        y_cond_mean (Callable): Conditional emission mean function.
        y_cond_cov (Callable): Conditional emission covariance function.
        x (D_in,): Control input.
        y (D_obs,): Emission.
        sv_threshold (float): Threshold for singular values.
        adaptive_variance (bool): Whether to use adaptive variance.

    Returns:
        m_cond (D_hid,): Posterior mean.
        U_cond (D_hid, D_mem,): Posterior basis.
        Sigma_cond (D_mem,): Posterior singular values.
    """
    m_Y = lambda w: y_cond_mean(w, x)
    Cov_Y = lambda w: y_cond_cov(w, x)
    
    yhat = jnp.atleast_1d(m_Y(m))
    if adaptive_variance:
        R = jnp.eye(yhat.shape[0]) * obs_noise_var
    else:
        R = jnp.atleast_2d(Cov_Y(m))
    L = jnp.linalg.cholesky(R)
    A = jnp.linalg.lstsq(L, jnp.eye(L.shape[0]))[0].T
    H = _jacrev_2d(m_Y, m)
    W_tilde = jnp.hstack([Sigma * U, (H.T @ A).reshape(U.shape[0], -1)])
    
    # Update the U matrix
    u, lamb, _ = jnp.linalg.svd(W_tilde, full_matrices=False)
    U_cond, U_extra = u[:, :U.shape[1]], u[:, U.shape[1]:]
    Sigma_cond, Sigma_extra = lamb[:U.shape[1]], lamb[U.shape[1]:]
    W_extra = Sigma_extra * U_extra
    Ups_cond = Ups + jnp.einsum('ij,ij->i', W_extra, W_extra)[:, jnp.newaxis]
    
    G = jnp.linalg.pinv(jnp.eye(W_tilde.shape[1]) + W_tilde.T @ (W_tilde/Ups))
    K = (H.T @ A) @ A.T/Ups - (W_tilde/Ups @ G) @ ((W_tilde/Ups).T @ (H.T @ A) @ A.T)
    m_cond = m + K @ (y - yhat)
    
    return m_cond, U_cond, Sigma_cond, Ups_cond


# Orthogonal LOFI --------------------------------------------------------------

def _lofi_orth_condition_on(
    m: Float[Array, "state_dim"],
    U: Float[Array, "state_dim memory_size"],
    Lambda: Float[Array, "memory_size"],
    eta: float,
    y_cond_mean: Callable,
    y_cond_cov: Callable,
    x: Float[Array, "input_dim"],
    y: Float[Array, "obs_dim"],
    adaptive_variance: bool = False,
    obs_noise_var: float = 1.0,
    key: int = 0
):
    """Condition step of the low-rank filter algorithm based on orthogonal SVD method.

    Args:
        m (D_hid,): Prior mean.
        U (D_hid, D_mem,): Prior basis.
        Lambda (D_mem,): Prior singular values.
        eta (float): Prior precision.
        y_cond_mean (Callable): Conditional emission mean function.
        y_cond_cov (Callable): Conditional emission covariance function.
        x (D_in,): Control input.
        y (D_obs,): Emission.
        adaptive_variance (bool): Whether to use adaptive variance.
        obs_noise_var (float): Observation noise variance.
        key (int): Random key.

    Returns:
        m_cond (D_hid,): Posterior mean.
        U_cond (D_hid, D_mem,): Posterior basis.
        Lambda_cond (D_mem,): Posterior singular values.
    """
    if isinstance(key, int) or len(key.shape) < 1:
        key = jr.PRNGKey(key)
    P, L = U.shape
    
    m_Y = lambda w: y_cond_mean(w, x)
    Cov_Y = lambda w: y_cond_cov(w, x)

    yhat = jnp.atleast_1d(m_Y(m))    
    C = yhat.shape[0]
    
    if adaptive_variance:
        R = jnp.eye(C) * obs_noise_var
    else:
        R = jnp.atleast_2d(Cov_Y(m))
    R_chol = jnp.linalg.cholesky(R)
    A = jnp.linalg.lstsq(R_chol, jnp.eye(C))[0].T
    H = _jacrev_2d(m_Y, m)
    W_tilde = jnp.hstack([Lambda * U, (H.T @ A).reshape(P, -1)])
    S = eta*jnp.eye(W_tilde.shape[1]) + W_tilde.T @ W_tilde
    K = (H.T @ A) @ A.T - W_tilde @ (_invert_2x2_block_matrix(S, C) @ (W_tilde.T @ ((H.T @ A) @ A.T)))

    # Update the basis and singular values
    def _update_basis(carry, i):
        U, Lambda = carry
        U_tilde = (H.T - U @ (U.T @ H.T)) @ A
        v = U_tilde[:, i]
        u = _normalize(v)
        U_cond = jnp.where(Lambda.min() < u @ v, U.at[:, Lambda.argmin()].set(u), U)
        Lambda_cond = jnp.where(Lambda.min() < u @ v, Lambda.at[Lambda.argmin()].set(u.T @ v), Lambda)
        
        return (U_cond, Lambda_cond), (U_cond, Lambda_cond)

    perm = jr.permutation(key, C)
    (U_cond, Lambda_cond), _ = scan(_update_basis, (U, Lambda), perm)
    
    # Update the mean
    m_cond = m + K/eta @ (y - yhat)

    return m_cond, U_cond, Lambda_cond