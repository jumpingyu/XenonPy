# Copyright 2018 TsumiNa. All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.
from warnings import warn

import numpy as np
import torch as tc
import torch.nn as nn
from pandas import DataFrame as df
from scipy.stats import pearsonr
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.externals import joblib
from sklearn.metrics import mean_absolute_error, r2_score
from torch.autograd import Variable as Var

from .checker import Checker
from .wrap import Init


class ModelRunner(BaseEstimator, RegressorMixin):
    """
    Run model.
    """

    def __init__(self, epochs=2000, *,
                 ctx='cpu',
                 check_step=100,
                 log_step=0,
                 work_dir=None,
                 verbose=True
                 ):
        """

        Parameters
        ----------
        epochs: int
        log_step: int
        ctx: str
        check_step: int
        work_dir: str
        verbose: bool
            Print :class:`ModelRunner` environment.
        """
        self.verbose = verbose
        self.check_step = check_step
        self.ctx = ctx
        self.epochs = epochs
        self.log_step = log_step
        self.work_dir = work_dir
        self.checker = None
        self.model = None
        self.model_name = None
        self.loss_func = None
        self.optim = None
        self.lr = None
        self.lr_scheduler = None

    def __enter__(self):
        if self.verbose:
            print('Runner environment:')
            print('Running dir: {}'.format(self.work_dir))
            print('Epochs: {}'.format(self.epochs))
            print('Context: {}'.format(self.ctx.upper()))
            print('Check step: {}'.format(self.check_step))
            print('Log step: {}\n'.format(self.log_step))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        del self

    def __call__(self, model, name=None, *,
                 init_weight=Init.uniform(scale=0.1),
                 loss_func=None,
                 optim=None,
                 lr=0.001,
                 lr_scheduler=None):

        def _init_weight(m):
            if isinstance(m, nn.Linear):  # fixme: not only linear
                print('init weight -> {}'.format(m))
                init_weight(m.weight)

        # model must inherit form nn.Module
        if not isinstance(model, nn.Module):
            raise ValueError(
                'Runner need a `torch.nn.Module` instance as first parameter but got {}'.format(type(model)))

        if not name:
            from hashlib import md5
            sig = md5(model.__str__().encode()).hexdigest()
            name = sig
        if init_weight:
            model.apply(_init_weight)
        self.checker = Checker(name, self.work_dir)
        self.model = model
        self.model_name = name
        self.loss_func = loss_func
        self.optim = optim
        self.lr = lr
        self.lr_scheduler = lr_scheduler
        self.checker(init_model=model)

    @staticmethod
    def _d2tv(data):
        if isinstance(data, df):
            data = tc.from_numpy(data.as_matrix()).type(tc.FloatTensor)
        elif isinstance(data, np.ndarray):
            data = tc.from_numpy(data).type(tc.FloatTensor)
        else:
            raise ValueError('need to be <numpy.ndarray> or <pandas.DataFrame> but got {}'.format(type(data)))
        return Var(data, requires_grad=False)

    def fit(self, x_train, y_train):
        """
        Fit Neural Network model

        Parameters
        ----------
        x_train: ``numpy.ndarray`` or ``pandas.DataFrame``
            Training data.
        y_train: ``numpy.ndarray`` or ``pandas.DataFrame``
            Target values.

        Returns
        -------
        self:
            returns an instance of self.
        """
        self.checker(x_train=x_train, y_train=y_train)

        # transform to torch tensor
        x_train = self._d2tv(x_train)
        y_train = self._d2tv(y_train)

        # if use CUDA acc
        if self.ctx.lower() == 'gpu':
            if tc.cuda.is_available():
                self.model.cuda()
                x_train = x_train.cuda()
                y_train = y_train.cuda()
            else:
                warn('No cuda environment, use cpu fallback.', RuntimeWarning)
        else:
            self.model.cpu()

        # optimization
        optim = self.optim(self.model.parameters(), lr=self.lr)

        # adjust learning rate
        scheduler = None
        if self.lr_scheduler is not None:
            scheduler = self.lr_scheduler(optim)

        # train
        loss, y_pred = None, None
        print('=======start training=======')
        print('Model name: {}\n'.format(self.model_name))
        for t in range(self.epochs):
            if scheduler and not isinstance(scheduler, tc.optim.lr_scheduler.ReduceLROnPlateau):
                scheduler.step()
            y_pred = self.model(x_train)
            loss = self.loss_func(y_pred, y_train)
            if scheduler and isinstance(scheduler, tc.optim.lr_scheduler.ReduceLROnPlateau):
                scheduler.step(loss)
            optim.zero_grad()
            loss.backward()
            optim.step()

            if self.log_step > 0 and t % self.log_step == 0:
                print('at step[{}/{}], Loss={:.4f}'.format(t, self.epochs, loss.data[0]))
            if self.check_step > 0 and t % self.check_step == 0:
                self.checker(model_state=self.model.state_dict(),
                             epochs=t,
                             loss=loss.data[0])

        print('\nFinal loss={:.4f}'.format(loss.data[0]))
        print('=======over training=======\n')

        # save last results
        self.checker(model_state=self.model.state_dict(),
                     epochs=self.epochs,
                     loss=loss.data[0],
                     trained_model=self.model)
        return self

    def predict(self, x_test, y_test):
        self.checker(x_test=x_test, y_test=y_test)
        # prepare x
        x_test = self._d2tv(x_test)

        # if use CUDA acc
        if self.ctx.lower() == 'gpu':
            if tc.cuda.is_available():
                self.model.cuda()
                x_test = x_test.cuda()
            else:
                warn('No cuda environment, use cpu fallback.', RuntimeWarning)
        else:
            self.model.cpu()
        # prediction
        y_true, y_pred = y_test.ravel(), self.model(x_test).cpu().data.numpy().ravel()

        mae = mean_absolute_error(y_true, y_pred)
        r2 = r2_score(y_true, y_pred)
        pr, p_val = pearsonr(y_true, y_pred)
        self.checker(summary={'layers': str(self.model),
                              'name': self.checker.name,
                              'mae': mae,
                              'r2': r2,
                              'pearsonr': pr,
                              'p-value': p_val})

        return y_true, y_pred

    def from_checkpoint(self, fname):
        raise NotImplementedError('Not implemented')

    def dump(self, fpath, **kwargs):
        """
        Save model into pickled file with at ``fpath``.
        Some additional description can be given from ``kwargs``.

        Parameters
        ----------
        fpath: str
            Path with name of pickle file.
        kwargs: dict
            Additional description
        """
        val = dict(model=self.model, **kwargs)
        joblib.dump(val, fpath)
