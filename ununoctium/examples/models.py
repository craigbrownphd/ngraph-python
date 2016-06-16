from neon.util.argparser import NeonArgparser
from neon.data import ImageLoader
import geon.backends.graph.dataloaderbackend
from neon.initializers import Uniform, Constant

import geon.backends.graph.funs as be
import geon.backends.graph.graph as graph
import geon.backends.graph.evaluation as evaluation
import numpy as np

# parse the command line arguments (generates the backend)
parser = NeonArgparser(__doc__)
parser.set_defaults(backend='dataloader')
parser.add_argument('--subset_pct', type=float, default=100,
                    help='subset of training dataset to use (percentage)')
args = parser.parse_args()

@be.with_name_scope
def linear(ns, x, axes, init=None, bias=None):
    ns.weights = be.Parameter(axes=be.linear_map_axes(be.sample_axes(x.axes), be.sample_axes(axes)), init=init)
    result = be.dot(ns.weights, x)
    if bias is not None:
        ns.bias = be.Parameter(axes=result.axes.sample, init=bias)
        result = result + ns.bias
    return result


def affine(x, activation, **kargs):
    return activation(linear(x, **kargs))


@be.with_name_scope
def mlp(ns, x, activation, shape_spec, axes, **kargs):
    value = x
    with be.name_scope_list('L') as name_scopes:
        for hidden_activation, hidden_axes, hidden_shapes in shape_spec:
            for shape in hidden_shapes:
                with be.next_name_scope(name_scopes) as nns:
                    nns.axes = tuple(be.AxisVar(like=axis, length=length) for axis, length in zip(hidden_axes, shape))
                    value = affine(value, activation=hidden_activation, axes=nns.axes, **kargs)
        with be.next_name_scope(name_scopes):
            value = affine(value, activation=activation, axes=axes, **kargs)
    return value


def L2(x):
    return be.dot(x, x)


def cross_entropy(y, t):
    """

    :param y:  Estimate
    :param t: Actual 1-hot data
    :return:
    """
    return -be.sum(be.log(y) * t)


class MyTest(be.Model):
    def __init__(self, **kargs):
        super(MyTest, self).__init__(**kargs)

        uni = Uniform(-.001, .001)

        g = self.graph

        g.C = be.AxisVar()
        g.H = be.AxisVar()
        g.W = be.AxisVar()
        g.N = be.AxisVar()
        g.Y = be.AxisVar()

        be.set_batch_axes([g.N])

        g.x = be.input(axes=(g.C, g.H, g.W, g.N))
        g.y = be.input(axes=(g.Y, g.N))

        #layers = [(be.tanh, (g.H, g.W), [(16, 16)] * 1 + [(4, 4)])]
        layers = [(be.tanh, (g.Y,), [(200,)])]

        g.value = mlp(g.x, activation=be.softmax, shape_spec=layers, axes=g.y.axes, init=uni)

        g.error = cross_entropy(g.value, g.y)

        # L2 regularizer of parameters
        reg = None
        for param in g.value.parameters():
            l2 = L2(param)
            if reg is None:
                reg = l2
            else:
                reg = reg + l2

        g.loss = g.error + .01 * reg


    @be.with_graph_scope
    def dump(self):
        g = self.graph

        g.x.value = np.empty((3, 32, 32, 128))
        g.y.value = np.empty((1000, 128))

        learning_rate = be.input(axes=())
        params = g.error.parameters()
        derivs = [be.deriv(g.error, param) for param in params]

        gnp = evaluation.GenNumPy(results=derivs)
        gnp.evaluate()

    @be.with_graph_scope
    def train(self):
        with be.bound_environment() as env:
            # setup data provider
            imgset_options = dict(inner_size=32, scale_range=40, aspect_ratio=110,
                                  repo_dir=args.data_dir, subset_pct=args.subset_pct)

            train = ImageLoader(set_name='train', shuffle=True, **imgset_options)
            #train = ImageLoader(set_name='train', shuffle=False, do_transforms=False, **imgset_options)
            test = ImageLoader(set_name='validation', shuffle=False, do_transforms=False, **imgset_options)

            graph = self.graph
            graph.N.length = train.bsz
            c, h, w = train.shape
            graph.C.length = c
            graph.H.length = h
            graph.W.length = w
            graph.Y.length = train.nclasses

            learning_rate = be.input(axes=())
            params = graph.error.parameters()
            derivs = [be.deriv(graph.loss, param) for param in params]

            updates = be.doall(all=[be.decrement(param, learning_rate * deriv) for param, deriv in zip(params, derivs)])

            enp = evaluation.NumPyEvaluator(results=[self.graph.value, graph.error, updates]+derivs)
            enp.initialize()

            for epoch in range(args.epochs):
                print("Epoch {epoch}".format(epoch=epoch))
                training_error = 0
                training_n = 0
                learning_rate.value = .1 / (1 + epoch) / train.bsz
                for mb_idx, (xraw, yraw) in enumerate(train):
                    graph.x.value = xraw.array
                    graph.y.value = yraw.array
                    vals = enp.evaluate()
                    training_error += vals[graph.error]/train.bsz
                    training_n += 1
                    # break

                print('Training error: {e}'.format(e=training_error/training_n))
                self.test(env, test)

                train.reset()

            return env

    @be.with_graph_scope
    def test(self, env, test):
        graph = self.graph
        with be.bound_environment(env):
            enp = evaluation.NumPyEvaluator(results=[self.graph.value, graph.error])
            total_error = 0
            n = 0
            for mb_idx, (xraw, yraw) in enumerate(test):
                graph.x.value = xraw.array
                graph.y.value = yraw.array
                vals = enp.evaluate()
                total_error += vals[graph.error] / test.bsz
                n += 1
                # break
            print("Test error: {e}".format(e=total_error/n))


y = MyTest()
#y.dump()
env = y.train()
#y.test(env)
