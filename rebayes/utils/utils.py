from functools import partial
from typing import Sequence

import flax.linen as nn
import jax
from jax import jacrev, jit
from jax.experimental import host_callback
from jax.flatten_util import ravel_pytree
import jax.numpy as jnp
import jax.random as jr
import numpy as np
import optax

from dynamax.generalized_gaussian_ssm.models import ParamsGGSSM


# .8796... is stddev of standard normal truncated to (-2, 2)
TRUNCATED_STD = 20.0 / np.array(.87962566103423978)
_jacrev_2d = lambda f, x: jnp.atleast_2d(jacrev(f)(x))


# ------------------------------------------------------------------------------
# NN Models

class CNN(nn.Module):
    output_dim: int = 10
    activation: nn.Module = nn.relu
    
    @nn.compact
    def __call__(self, x):
        x = nn.Conv(features=32, kernel_size=(3, 3))(x)
        x = self.activation(x)
        x = nn.avg_pool(x, window_shape=(2, 2), strides=(2, 2))
        x = nn.Conv(features=64, kernel_size=(3, 3))(x)
        x = self.activation(x)
        x = nn.avg_pool(x, window_shape=(2, 2), strides=(2, 2))
        x = x.reshape((x.shape[0], -1))  # flatten
        x = nn.Dense(features=128)(x)
        x = self.activation(x)
        x = nn.Dense(features=self.output_dim)(x)
        
        return x
    

class MLP(nn.Module):
    features: Sequence[int]
    activation: nn.Module = nn.relu

    @nn.compact
    def __call__(self, x):
        x = x.ravel()
        for feat in self.features[:-1]:
            x = self.activation(nn.Dense(feat)(x))
        x = nn.Dense(self.features[-1])(x)
        
        return x
    

def scaling_factor(model_dims, bias_weight_cov_ratio):
    """This is the factor that is used to scale the
    standardized parameters back into the original space."""
    features = np.array(model_dims)
    biases = features[1:]
    fan_ins = features[:-1]
    num_kernels = features[:-1] * features[1:]
    bias_fanin_kernels = zip(biases, fan_ins, num_kernels)
    factors = []
    for term in bias_fanin_kernels:
        bias, fan_in, num_kernel = (x.item() for x in term)
        factors.extend([1.0] * bias)
        factors.extend([bias_weight_cov_ratio/np.sqrt(fan_in)] * num_kernel)
    factors = np.array(factors).ravel()

    return factors    


def get_mlp_flattened_params(model_dims, key=0, activation=nn.relu, rescale=False, 
                             bias_weight_cov_ratio=TRUNCATED_STD):
    """Generate MLP model, initialize it using dummy input, and
    return the model, its flattened initial parameters, function
    to unflatten parameters, and apply function for the model.
    Args:
        model_dims (List): List of [input_dim, hidden_dim, ..., output_dim]
        key (PRNGKey): Random key. Defaults to 0.
    Returns:
        model: MLP model with given feature dimensions.
        flat_params: Flattened parameters initialized using dummy input.
        unflatten_fn: Function to unflatten parameters.
        apply_fn: fn(flat_params, x) that returns the result of applying the model.
    """
    if isinstance(key, int):
        key = jr.PRNGKey(key)

    # Define MLP model
    input_dim, features = model_dims[0], model_dims[1:]
    model = MLP(features, activation)
    if isinstance(input_dim, int):
        dummy_input = jnp.ones((input_dim,))
    else:
        dummy_input = jnp.ones((*input_dim,))
        model_dims = [np.prod(input_dim)] + model_dims[1:]

    # Initialize parameters using dummy input
    params = model.init(key, dummy_input)
    flat_params, unflatten_fn = ravel_pytree(params)
    
    scaling = scaling_factor(model_dims, bias_weight_cov_ratio) if rescale else 1.0
    flat_params = flat_params / scaling
    rec_fn = lambda x: unflatten_fn(x * scaling)
    
    # Define apply function
    @jit
    def apply_fn(flat_params, x):
        return model.apply(rec_fn(flat_params), jnp.atleast_1d(x))

    return model, flat_params, rec_fn, apply_fn


def init_model(key=0, type='cnn', features=(400, 400, 10), classification=True, rescale=False):
    if isinstance(key, int):
        key = jr.PRNGKey(key)
    input_dim = [1, 28, 28, 1]
    model_dim = [input_dim, *features]
    if type == 'cnn':
        if classification:
            model = CNN()
        else:
            model = CNN(output_dim=1)
        params = model.init(key, jnp.ones(input_dim))['params']
        flat_params, unflatten_fn = ravel_pytree(params)
        apply_fn = lambda w, x: model.apply({'params': unflatten_fn(w)}, x).ravel()

        emission_mean_function = apply_fn
    elif type == 'mlp':
        model, flat_params, _, apply_fn = \
            get_mlp_flattened_params(model_dim, key, rescale=rescale, zero_ll=zero_ll,
                                     bias_weight_cov_ratio=bias_weight_cov_ratio)
            
    else:
        raise ValueError(f'Unknown model type: {type}')
    
    model_dict = {
        'model': model,
        'flat_params': flat_params,
        'apply_fn': apply_fn,
    }
    
    if classification:
        if features[-1] == 1:
            # Binary classification
            sigmoid_fn = lambda w, x: jnp.clip(jax.nn.sigmoid(apply_fn(w, x)), 1e-4, 1-1e-4).ravel()
            emission_mean_function = lambda w, x: sigmoid_fn(w, x)
            emission_cov_function = lambda w, x: sigmoid_fn(w, x) * (1 - sigmoid_fn(w, x))
        else:
            # Multiclass classification
            emission_mean_function=lambda w, x: jax.nn.softmax(apply_fn(w, x))
            def emission_cov_function(w, x):
                ps = emission_mean_function(w, x)
                return jnp.diag(ps) - jnp.outer(ps, ps) + 1e-3 * jnp.eye(len(ps)) # Add diagonal to avoid singularity
            
            def replay_emission_cov_function(w, w_lin, x):
                m_Y = lambda w: emission_mean_function(w, x)
                H = _jacrev_2d(m_Y, w_lin)
                ps = jnp.atleast_1d(m_Y(w_lin)) + H @ (w - w_lin)
                return jnp.diag(ps) - jnp.outer(ps, ps) + 1e-3 * jnp.eye(len(ps)) # Add diagonal to avoid singularity
            model_dict["replay_emission_cov_function"] = replay_emission_cov_function
        model_dict['emission_mean_function'] = emission_mean_function
        model_dict['emission_cov_function'] = emission_cov_function
    else:
        # Regression
        emission_mean_function = apply_fn
        model_dict['emission_mean_function'] = emission_mean_function
    
    return model_dict


# ------------------------------------------------------------------------------
# EKF

def initialize_params(flat_params, predict_fn):
    state_dim = flat_params.size
    fcekf_params = ParamsGGSSM(
        initial_mean=flat_params,
        initial_covariance=jnp.eye(state_dim),
        dynamics_function=lambda w, _: w,
        dynamics_covariance = jnp.eye(state_dim) * 1e-4,
        emission_mean_function = lambda w, x: predict_fn(w, x),
        emission_cov_function = lambda w, x: predict_fn(w, x) * (1 - predict_fn(w, x))
    )

    def callback(bel, t, x, y):
        return bel.mean

    return fcekf_params, callback


# ------------------------------------------------------------------------------
# SGD

def fit_optax(params, optimizer, input, output, loss_fn, num_epochs, return_history=False):
    opt_state = optimizer.init(params)

    @jax.jit
    def step(params, opt_state, x, y):
        loss_value, grads = jax.value_and_grad(loss_fn)(params, x, y)
        updates, opt_state = optimizer.update(grads, opt_state, params)
        params = optax.apply_updates(params, updates)
        return params, opt_state, loss_value

    if return_history:
        params_history=[]

    for epoch in range(num_epochs):
        for i, (x, y) in enumerate(zip(input, output)):
            params, opt_state, loss_value = step(params, opt_state, x, y)
            if return_history:
                params_history.append(params)

    if return_history:
        return jnp.array(params_history)
    return params


# Generic loss function
def loss_optax(params, x, y, loss_fn, apply_fn):
    y, y_hat = jnp.atleast_1d(y), apply_fn(params, x)
    loss_value = loss_fn(y, y_hat)
    return loss_value.mean()


# Define SGD optimizer
sgd_optimizer = optax.sgd(learning_rate=1e-2)


def tree_to_cpu(tree):
    return jax.tree_map(np.array, tree)


def get_subtree(tree, key):
    return jax.tree_map(lambda x: x[key], tree, is_leaf=lambda x: key in x)


def eval_runs(key, num_runs_pc, agent, model, train, test, eval_callback, test_kwargs):
    X_learn, y_learn = train
    _, dim_in = X_learn.shape

    num_devices = jax.device_count()
    num_sims = num_runs_pc * num_devices
    keys = jax.random.split(key, num_sims).reshape(-1, num_devices, 2)
    n_vals = len(X_learn)

    @partial(jax.pmap, in_axes=1)
    @partial(jax.vmap, in_axes=0)
    def evalf(key):
        key_shuffle, key_init = jax.random.split(key)
        ixs_shuffle = jax.random.choice(key_shuffle, n_vals, (n_vals,), replace=False)

        params = model.init(key_init, jnp.ones((1, dim_in)))
        flat_params, _ = ravel_pytree(params)

        bel, output = agent.scan(
            X_learn[ixs_shuffle], y_learn[ixs_shuffle], callback=eval_callback, progress_bar=False, **test_kwargs
        )

        return output
    outputs = evalf(keys)
    outputs = jax.tree_map(lambda x: x.reshape(num_sims, -1), outputs)
    
    return outputs


def symmetrize_matrix(A):
    """Symmetrize a matrix."""
    return (A + A.T) / 2