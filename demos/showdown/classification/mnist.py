import os
from pathlib import Path
from functools import partial
import pickle

import jax
import jax.random as jr
import jax.numpy as jnp
from jax import jit
from jax.flatten_util import ravel_pytree
import optax

from rebayes.low_rank_filter.lofi import LoFiParams
from demos.showdown.classification import classification_train as benchmark
from demos.showdown.classification import hparam_tune_clf as hpt


def load_data():
    train_ds, test_ds = benchmark.load_fmnist_datasets()
    X_train, y_train = jnp.array(train_ds['image']), jnp.array(train_ds['label'])
    X_test, y_test = jnp.array(test_ds['image']), jnp.array(test_ds['label'])
    
    # Reshape data
    X_train = X_train.reshape(-1, 1, 28, 28, 1)
    y_train_ohe = jax.nn.one_hot(y_train, 10) # one-hot encode labels
    
    dataset = {
        'train': (X_train, y_train_ohe),
        'test': (X_test, y_test)
    }
    
    return dataset


def init_cnn(key=0):
    if isinstance(key, int):
        key = jr.PRNGKey(key)
        
    cnn_model = benchmark.CNN()
    params = cnn_model.init(key, jnp.ones([1, 28, 28, 1]))['params']
    flat_params, unflatten_fn = ravel_pytree(params)
    apply_fn = lambda w, x: cnn_model.apply({'params': unflatten_fn(w)}, x).ravel()
    
    emission_mean_function=lambda w, x: jax.nn.softmax(apply_fn(w, x))
    def emission_cov_function(w, x):
        ps = emission_mean_function(w, x)
        return jnp.diag(ps) - jnp.outer(ps, ps) + 1e-3 * jnp.eye(len(ps)) # Add diagonal to avoid singularity
    
    model = {
        'model': cnn_model,
        'flat_params': flat_params,
        'apply_fn': apply_fn,
        'emission_mean_function': emission_mean_function,
        'emission_cov_function': emission_cov_function,
    }
    
    return model


def train_agent(model, dataset, output_path, agent_type='fdekf', **kwargs):
    print(f'Training {agent_type} agent...')
    X_train, y_train = dataset['train']
    train = (X_train[:1000], y_train[:1000])
    cnn_model, emission_mean_function, emission_cov_function = \
        model['model'], model['emission_mean_function'], model['emission_cov_function']
    
    if agent_type == 'rsgd':
        pbounds = {
            'log_lr': (-8.0, 0.0),
            'n_inner': (1, 100),
        }
    else:
        pbounds={
            'log_init_cov': (-10, 0),
            'log_dynamics_weights': (-50, -50),
            'log_dynamics_cov': (-40, 0),
            'log_alpha': (-40, 0),
        }
        if agent_type == 'lofi_sph' or agent_type == 'lofi_diag':
            agent_type = 'lofi'
    
    ll_callback = partial(benchmark.eval_callback, evaluate_fn=benchmark.mnist_evaluate_ll)
    optimizer, *_ = hpt.create_optimizer(
        cnn_model, pbounds, 314, train, dataset['test'], emission_mean_function,
        emission_cov_function, callback=ll_callback, method=agent_type, verbose=0, **kwargs
    )
    
    optimizer.maximize(
        init_points=10,
        n_iter=15,
    )
    best_hparams = hpt.get_best_params(optimizer, method=agent_type)
    print(f"Best target: {optimizer.max['target']}")
    
    estimator, bel = hpt.build_estimator(
        model['flat_params'],
        model['apply_fn'],
        best_hparams,
        emission_mean_function,
        emission_cov_function,
        method=agent_type,
        **kwargs,
    )
    
    nll_callback = partial(benchmark.eval_callback, evaluate_fn=benchmark.mnist_evaluate_nll)
    nll_mean, nll_std = benchmark.mnist_eval_agent(
        dataset['train'], dataset['test'], model['apply_fn'], callback=nll_callback,
        agent=estimator, bel_init=bel,
    )
    
    miscl_callback = partial(benchmark.eval_callback, evaluate_fn=benchmark.mnist_evaluate_miscl)
    miscl_mean, miscl_std = benchmark.mnist_eval_agent(
        dataset['train'], dataset['test'], model['apply_fn'], callback=miscl_callback,
        agent=estimator, bel_init=bel,
    )
    
    nll_result = jax.block_until_ready({
        'mean': nll_mean,
        'std': nll_std,
    })
    miscl_result = jax.block_until_ready({
        'mean': miscl_mean,
        'std': miscl_std,
    })
    print('\n')
    
    return nll_result, miscl_result


if __name__ == "__main__":
    output_path = os.environ.get("REBAYES_OUTPUT")
    if output_path is None:
        output_path = Path(Path.cwd(), "output", "mnist")
        output_path.mkdir(parents=True, exist_ok=True)
    print(f"Output path: {output_path}")
    
    dataset = load_data() # load data
    cnn_model = init_cnn() # initialize model
    
    lofi_params_spherical = LoFiParams(
        memory_size=20,
        diagonal_covariance=False,
    )
    lofi_params_diagonal = LoFiParams(
        memory_size=20,
        diagonal_covariance=True,
    )
    
    agents = {
        'fdekf': None,
        'lofi_orth': {
            'lofi_params': lofi_params_spherical
        },
        'lofi_sph': {
            'lofi_params': lofi_params_spherical
        },
        'lofi_diag': {
            'lofi_params': lofi_params_diagonal
        },
        'rsgd': {
            'loss_fn': optax.softmax_cross_entropy,
            'buffer_size': 20,
            'dim_output': 10,
        }
    }
    
    nll_results, miscl_results = {}, {}
    for agent, kwargs in agents.items():
        if kwargs is None:
            nll, miscl = train_agent(cnn_model, dataset, output_path, agent_type=agent)
        else:
            nll, miscl = train_agent(cnn_model, dataset, output_path, agent_type=agent, **kwargs)
        nll_results[agent] = nll
        miscl_results[agent] = miscl
        
    # Store results and plot
    benchmark.store_results(nll_results, 'mnist_nll', output_path)
    benchmark.store_results(miscl_results, 'mnist_miscl', output_path)
    
    nll_title = "Test-set average NLL"
    benchmark.plot_results(nll_results, "mnist_nll", output_path, ylim=(0.5, 2.5), title=nll_title)
    
    miscl_title = "Test-set average misclassification rate"
    benchmark.plot_results(miscl_results, "mnist_miscl", output_path, ylim=(0.2, 0.8), title=miscl_title)