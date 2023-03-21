import pytest

from rebayes.base import RebayesParams
from rebayes.low_rank_filter.lofi import LoFiParams, INFLATION_METHODS
from rebayes.low_rank_filter.lofi_orthogonal import RebayesLoFiOrthogonal
from rebayes.low_rank_filter.lofi_spherical import RebayesLoFiSpherical
from rebayes.low_rank_filter.lofi_diagonal import RebayesLoFiDiagonal
from rebayes.low_rank_filter.test_orfit import load_rmnist_data
from rebayes.utils.utils import get_mlp_flattened_params


def setup_lofi(memory_size, steady_state, inflation_type):
    input_dim, hidden_dims, output_dim = 784, [2, 2], 1
    model_dims = [input_dim, *hidden_dims, output_dim]
    _, flat_params, _, apply_fn = get_mlp_flattened_params(model_dims)
    
    model_params = RebayesParams(
        initial_mean = flat_params,
        initial_covariance = 1e-1,
        dynamics_weights = 1.0,
        dynamics_covariance = 1e-1,
        emission_mean_function = apply_fn,
        emission_cov_function = None,
        adaptive_emission_cov = True,
        dynamics_covariance_inflation_factor = 1e-5,
    )

    lofi_params = LoFiParams(
        memory_size = memory_size,
        steady_state = steady_state,
        inflation = inflation_type,
    )
    
    return model_params, lofi_params


@pytest.mark.parametrize(
    "memory_size, steady_state, inflation_type",
    [(10, ss, it) for ss in [True, False] for it in INFLATION_METHODS]
)
def test_lofi_orthogonal(memory_size, steady_state, inflation_type):
    # Load rotated MNIST dataset
    n_train = 200
    X_train, y_train = load_rmnist_data(n_train)
    
    # Define mean callback function
    def callback(bel, *args, **kwargs):
        return bel.mean
    
    # Test if run without error
    model_params, lofi_params = setup_lofi(memory_size, steady_state, inflation_type)
    lofi_estimator = RebayesLoFiOrthogonal(
        model_params = model_params,
        lofi_params = lofi_params,
    )
    _ = lofi_estimator.scan(X_train, y_train, callback)
    
    assert True
    

@pytest.mark.parametrize(
    "memory_size, steady_state, inflation_type",
    [(10, ss, it) for ss in [True, False] for it in INFLATION_METHODS]
)
def test_lofi_spherical(memory_size, steady_state, inflation_type):
    # Load rotated MNIST dataset
    n_train = 200
    X_train, y_train = load_rmnist_data(n_train)
    
    # Define mean callback function
    def callback(bel, *args, **kwargs):
        return bel.mean
    
    # Test if run without error
    model_params, lofi_params = setup_lofi(memory_size, steady_state, inflation_type)
    lofi_estimator = RebayesLoFiSpherical(
        model_params = model_params,
        lofi_params = lofi_params,
    )
    _ = lofi_estimator.scan(X_train, y_train, callback)
    
    assert True
    
    
@pytest.mark.parametrize(
    "memory_size, steady_state, inflation_type",
    [(10, ss, it) for ss in [True, False] for it in INFLATION_METHODS]
)
def test_lofi_diagonal(memory_size, steady_state, inflation_type):
    # Load rotated MNIST dataset
    n_train = 200
    X_train, y_train = load_rmnist_data(n_train)
    
    # Define mean callback function
    def callback(bel, *args, **kwargs):
        return bel.mean
    
    # Test if run without error
    model_params, lofi_params = setup_lofi(memory_size, steady_state, inflation_type)
    lofi_estimator = RebayesLoFiDiagonal(
        model_params = model_params,
        lofi_params = lofi_params,
    )
    _ = lofi_estimator.scan(X_train, y_train, callback)
    
    assert True