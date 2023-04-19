"""
Prepcocessing and data augmentation for the datasets.
"""
import os
import torchvision
import numpy as np
import jax.numpy as jnp
from typing import Union, Callable
from multiprocessing import Pool
from augly import image


class DataAugmentationFactory:
    """
    This is a base library to process / transform the elements of a numpy
    array according to a given function. To be used with gendist.TrainingConfig
    """
    def __init__(self, processor):
        self.processor = processor

    def __call__(self, img, configs, n_processes=90):
        return self.process_multiple_multiprocessing(img, configs, n_processes)

    def process_single(self, X, *args, **kwargs):
        """
        Process a single element.

        Paramters
        ---------
        X: np.array
            A single numpy array
        kwargs: dict/params
            Processor's configuration parameters
        """
        return self.processor(X, *args, **kwargs)

    def process_multiple(self, X_batch, configurations):
        """
        Process all elements of a numpy array according to a list
        of configurations.
        Each image is processed according to a configuration.
        """
        X_out = []

        for X, configuration in zip(X_batch, configurations):
            X_processed = self.process_single(X, **configuration)
            X_out.append(X_processed)

        X_out = np.stack(X_out, axis=0)
        return X_out

    def process_multiple_multiprocessing(self, X_dataset, configurations, n_processes):
        """
        Process elements in a numpy array in parallel.

        Parameters
        ----------
        X_dataset: array(N, ...)
            N elements of arbitrary shape
        configurations: list
            List of configurations to apply to each element. Each
            element is a dict to pass to the processor.
        n_processes: [int, None]
            Number of cores to use. If None, use all available cores.
        """
        num_elements = len(X_dataset)
        if type(configurations) == dict:
            configurations = [configurations] * num_elements

        if n_processes == 1:
            dataset_proc = self.process_multiple(X_dataset, configurations)
            return dataset_proc.reshape(num_elements, -1)

        dataset_proc = np.array_split(X_dataset, n_processes)
        config_split = np.array_split(configurations, n_processes)
        elements = zip(dataset_proc, config_split)

        with Pool(processes=n_processes) as pool:
            dataset_proc = pool.starmap(self.process_multiple, elements)
            dataset_proc = np.concatenate(dataset_proc, axis=0)
        pool.join()

        return dataset_proc.reshape(num_elements, -1)


def load_mnist(root="/tmp/data", download=True):
    mnist_train = torchvision.datasets.MNIST(root=root, train=True, download=download)
    images = np.array(mnist_train.data) / 255.0
    labels = mnist_train.targets

    mnist_test = torchvision.datasets.MNIST(root=root, train=False)
    images_test = np.array(mnist_test.data) / 255.0
    labels_test = mnist_test.targets

    train = (images, labels)
    test = (images_test, labels_test)
    return train, test


def rotate_mnist(X, angle):
    """
    Rotate an image by a given angle.
    We take the image to be a square of size 28x28.
    TODO: generalize to any size
    """
    X_shift = image.aug_np_wrapper(X, image.rotate, degrees=angle)
    size_im = X_shift.shape[0]
    size_pad = (28 - size_im) // 2
    size_pad_mod = (28 - size_im) % 2
    X_shift = np.pad(X_shift, (size_pad, size_pad + size_pad_mod))

    return X_shift

def generate_rotated_images(images, n_processes, minangle=0, maxangle=180, anglefn=None):
    n_configs = len(images)
    angles = anglefn(n_configs, minangle, maxangle)

    processer = DataAugmentationFactory(rotate_mnist)
    configs = [{"angle": float(angle)} for angle in angles]
    images_proc = processer(images, configs, n_processes=n_processes)
    return images_proc, angles


def generate_rotated_images_pairs(images, angles, n_processes=1):
    processer = DataAugmentationFactory(rotate_mnist)
    configs = [{"angle": float(angle)} for angle in angles]
    images_proc = processer(images, configs, n_processes=n_processes)
    return images_proc, angles


def load_rotated_mnist(
    anglefn: Callable,
    root: str = "/tmp/data",
    target_digit: Union[int, None] = None,
    minangle: int = 0,
    maxangle: int = 180,
    n_processes: Union[int, None] = 1,
    num_train: int = 5_000,
    frac_train: Union[float, None] = None,
    seed: int = 314,
    sort_by_angle: bool = False,
    num_test: int = None,
):
    """
    """
    if seed is not None:
        np.random.seed(seed)

    if n_processes is None:
        n_processes = max(1, os.cpu_count() - 2)

    train, test = load_mnist(root=root)
    (X_train, labels_train), (X_test, labels_test) = train, test

    if target_digit is not None:
        digits = [target_digit] if type(target_digit) == int else target_digit

        map_train = [label in digits for label in labels_train]
        map_test = [label in digits for label in labels_test]
        X_train = X_train[map_train]
        X_test = X_test[map_test]

        digits_train = labels_train[map_train]
        digits_test = labels_test[map_test]
    else:
        digits_train = labels_train
        digits_test = labels_test

    X = np.concatenate([X_train, X_test], axis=0)
    digits = np.concatenate([digits_train, digits_test], axis=0)

    (X, y) = generate_rotated_images(X, n_processes, minangle=minangle, maxangle=maxangle, anglefn=anglefn)

    X = jnp.array(X)
    y = jnp.array(y)

    if (frac_train is None) and (num_train is None):
        raise ValueError("Either frac_train or num_train must be specified.")
    elif (frac_train is not None) and (num_train is not None):
        raise ValueError("Only one of frac_train or num_train can be specified.")
    elif frac_train is not None:
        num_train = round(frac_train * len(X_train))

    X_train, y_train, digits_train = X[:num_train], y[:num_train], digits[:num_train]
    if num_test is not None:
        X_test, y_test, digits_test = X[num_train : num_train + num_test], y[num_train : num_train + num_test], digits[num_train : num_train + num_test]
    else:
        X_test, y_test, digits_test = X[num_train:], y[num_train:], digits[num_train:]

    if sort_by_angle:
        ix_sort = jnp.argsort(y_train)
        X_train = X_train[ix_sort]
        y_train = y_train[ix_sort]
        digits_train = digits_train[ix_sort]

    train = (X_train, y_train, digits_train)
    test = (X_test, y_test, digits_test)

    return train, test


def load_and_transform(
    anglefn: Callable,
    digits: list,
    num_train: int = 5_000,
    sort_by_angle: bool = True,
):
    """
    Function to load and transform the rotated MNIST dataset.
    """
    data = load_rotated_mnist(
        anglefn, target_digit=digits, sort_by_angle=sort_by_angle, num_train=num_train,
    )
    train, test = data
    X_train, y_train, labels_train = train
    X_test, y_test, labels_test = test

    ymean, ystd = y_train.mean().item(), y_train.std().item()

    if ystd > 0:
        y_train = (y_train - ymean) / ystd
        y_test = (y_test - ymean) / ystd

    dataset = {
        "train": (X_train, y_train, labels_train),
        "test": (X_test, y_test, labels_test),
    }

    res = {
        "dataset": dataset,
        "ymean": ymean,
        "ystd": ystd,
    }

    return res
