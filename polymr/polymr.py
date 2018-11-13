import itertools
import sys
import operator
import time
import logging
import json
import random

from .base import *
from .runner import MTRunner, Graph, Source
from .dataset import MemoryInput, DirectoryInput, TextInput, Chunker

class ValueEmitter(object):
    def __init__(self, datasets):
        self.datasets = datasets

    def stream(self):
        for _, v in self.datasets.read():
            yield v

    def read(self, k=None):
        if k is None:
            return list(self.stream())

        return list(itertools.islice(self.stream(), k))

    def __iter__(self):
        return self.stream()

    def delete(self):
        self.datasets.delete()

class PBase(object):
    def __init__(self, source, pmer):
        assert isinstance(source, Source)
        self.source = source
        self.pmer = pmer

    def run(self, name=None, **kwargs):
        if name is None:
            name = 'polymr/{}'.format(random.random())

        logging.debug("run source: %s", self.source)
        ds = self.pmer.runner(name, self.pmer.graph, **kwargs).run([self.source])
        return ValueEmitter(ds[0])

    def read(self, k=None, **kwargs):
        return self.run(**kwargs).read(k)

def _identity(k, v):
    yield k, v

class PMap(PBase):

    def __init__(self, source, pmer, agg=None):
        super(PMap, self).__init__(source, pmer)
        self.agg = [] if agg is None else agg

    def run(self, name=None, **kwargs):
        if len(self.agg) > 0:
            return self.checkpoint().run(name, **kwargs)
        else:
            return super(PMap, self).run(name, **kwargs)

    def _add_map(self, f):
        return PMap(self.source, self.pmer, self.agg + [Map(f)])

    def sample(self, prob):
        def _sample(k, v):
            if get_rand().random() < prob:
                yield k, v

        return self._add_map(_sample)
        
    def checkpoint(self, force=False, combiner=None, options=None):
        if len(self.agg) > 0 or force:
            aggs = [Map(_identity)] if len(self.agg) == 0 else self.agg[:]
            name = ' -> ' .join('{}'.format(a.mapper.__name__) for a in aggs)
            name = 'Stage {}: %s => %s' % (self.source, name)
            source, pmer = self.pmer._add_mapper([self.source], 
                    Map(fuse(aggs)), 
                    combiner=combiner,
                    name=name,
                    options=options)
            return PMap(source, pmer) 

        return self

    def map(self, f):
        def _map(k, v):
            yield k, f(v)

        return self._add_map(_map)

    def filter(self, f):
        def _filter(k, v):
            if f(v):
                yield k, v

        return self._add_map(_filter)

    def flat_map(self, f):
        def _flat_map(k, v):
            for vi in f(v):
                yield k, vi

        return self._add_map(_flat_map)

    def group_by(self, key, vf=lambda x: x):
        def _group_by(_key, value):
            yield key(value), vf(value)

        pm = self._add_map(_group_by).checkpoint()
        return PReduce(pm.source, pm.pmer)

    def a_group_by(self, key, vf=lambda x: x):
        def _a_group_by(_key, value):
            yield key(value), vf(value)

        # We don't checkpoint here!
        pm = self._add_map(_a_group_by)
        return ARReduce(pm)

    def fold_by(self, key, binop, value=lambda x: x, **options):
        return self.a_group_by(key, value).reduce(binop, **options)

    def sort_by(self, key, **options):
        def _sort_by(_key, value):
            yield key(value), value

        return self._add_map(_sort_by).checkpoint(options=options)

    def join(self, other):
        assert isinstance(other, PBase)
        me = self.checkpoint(True)
        if isinstance(other, PMap):
            other = other.checkpoint(True)

        pmer = Polymr(me.pmer.graph.union(other.pmer.graph))
        return PJoin(me.source, pmer, other.source)

    def count(self, key=lambda x: x, **options):
        return self.a_group_by(key, lambda v: 1) \
                .reduce(operator.add, **options)

    def mean(self, key=lambda x: 1, value=lambda x: x, **options):
        def _binop(x, y):
            return x[0] + y[0], x[1] + y[1]

        return self.a_group_by(key, lambda v: (value(v), 1)) \
                .reduce(_binop, **options) \
                .map(lambda x: (x[0], x[1][0] / float(x[1][1])))

    def inspect(self, prefix="", exit=False):
        def _inspect(k, v):
            print("{}: {}".format(prefix, v))
            yield k, v

        ins = self._add_map(_inspect)
        if exit:
            ins.run()
            sys.exit(0)

        return ins

    def cached(self, **options):
        # Run the pipeline, load it into memory, and create a new graph
        options['memory'] = True
        return self.checkpoint(options=options)

    def sink(self, path):
        aggs = [Map(_identity)] if len(self.agg) == 0 else self.agg[:]
        name = ' -> ' .join('{}'.format(a.mapper.__name__) for a in aggs)
        name = 'Stage {}: %s => %s' % (self.source, name)
        source, pmer = self.pmer._add_sink([self.source], 
                Map(fuse(aggs)), 
                path=path,
                name=name,
                options=None)
        return PMap(source, pmer) 

    def sink_tsv(self, path):
        return self.map(lambda x: u'\t'.join(str(p) for p in x)).sink(path)

    def sink_json(self, path):
        return self.map(json.dumps).sink(path)

    def cross_tiny_right(self, other, cross):
        assert isinstance(other, PMap)
        return other.cross_tiny_left(self, cross)

    def cross_tiny_left(self, other, cross, **options):
        def _cross(k1, v1, k2, v2):
            yield k1, cross(v1, v2)

        pmer = self.checkpoint()
        other = other.checkpoint()
        pmer = Polymr(self.pmer.graph.union(other.pmer.graph))
        name = 'Stage {}: (%s X %s)' % (self.source, other.source)
        source, pmer = pmer._add_mapper([other.source, self.source], 
                MapCrossJoin(_cross), 
                combiner=None,
                name=name,
                options=options)
        return PMap(source, pmer) 

        #return other.cross_tiny_right(self, cross, partitions)

class ARReduce(object):
    def __init__(self, pmap):
        self.pmap = pmap

    def reduce(self, binop, reduce_buffer=1000, **options):
        def _reduce(key, vs):
            acc = next(vs)
            for v in vs:
                acc = binop(acc, v)

            return acc

        red = Reduce(_reduce)
        options.update({"binop": binop, "reduce_buffer": reduce_buffer})
        # We add the associative aggregator to the combiner during map
        pm = self.pmap.checkpoint(True, 
                combiner=PartialReduceCombiner(red), 
                options=options)
        return PReduce(pm.source, pm.pmer).reduce(_reduce)
    
    def first(self, **options):
        return self.reduce(lambda x, _y: x, **options)

    def sum(self, **options):
        return self.reduce(lambda x, y: x + y, **options)

class PReduce(PBase):

    def reduce(self, f):
        new_source, pmer = self.pmer._add_reducer([self.source], KeyedReduce(f))
        return PMap(new_source, pmer)

    def unique(self, key=lambda x: x):
        def _uniq(k, it):
            seen = set()
            agg = []
            for v in it:
                fv = key(v)
                if fv not in seen:
                    seen.add(fv)
                    agg.append(v)

            return agg

        return self.reduce(_uniq)

    def join(self, other):
        assert isinstance(other, PBase)
        if isinstance(other, PMap):
            other = other.checkpoint(True)

        pmer = Polymr(self.pmer.graph.union(other.pmer.graph))
        return PJoin(self.source, pmer, other.source)

class PJoin(PBase):

    def __init__(self, source, pmer, right):
        super(PJoin, self).__init__(source, pmer)
        self.right = right

    def run(self, name=None, **kwargs):
        return self.reduce(lambda l, r: (list(l), list(r))).run(name, **kwargs)

    def reduce(self, aggregate):
        def _reduce(k, left, right):
            return aggregate(left, right)

        source, pmer = self.pmer._add_reducer([self.source, self.right], 
                KeyedInnerJoin(_reduce))
        return PMap(source, pmer)

    def left_reduce(self, aggregate):
        def _reduce(k, left, right):
            return aggregate(left, right)

        source, pmer = self.pmer._add_reducer([self.source, self.right], 
                KeyedLeftJoin(_reduce))
        return PMap(source, pmer)

    def _cross(self, crosser):
        def _cross(k1, v1, k2, v2):
            return k1, crosser(v1, v2)

        source, pmer = self.pmer._add_reducer([self.source, self.right],
                KeyedCrossJoin(_cross))

        return PMap(source, pmer).map(lambda x: x[1])

class Polymr(object):
    def __init__(self, graph=None, runner=None):
        if graph is None:
            graph = Graph()

        self.graph = graph 
        if runner is None:
            runner = MTRunner

        self.runner = runner

    @classmethod
    def memory(cls, items, partitions=50):
        mi = MemoryInput(list(enumerate(items)), partitions)
        source, ng = Graph().add_input(mi)
        return PMap(source, Polymr(ng))

    @classmethod
    def text(cls, fname, chunk_size=16*1024**2):
        if os.path.isdir(fname):
            inp = DirectoryInput(fname, chunk_size)
        else:
            inp = TextInput(fname, chunk_size)

        source, ng = Graph().add_input(inp)
        return PMap(source, Polymr(ng))

    @classmethod
    def json(cls, *args, **kwargs):
        return cls.text(*args, **kwargs).map(json.loads)

    @classmethod
    def from_dataset(cls, dataset):
        assert isinstance(dataset, Chunker)
        source, ng = Graph().add_input(dataset)
        return PMap(source, Polymr(ng))

    @classmethod
    def run(self, *pmers, **kwargs):
        sources = []
        graph = None
        for i, pmer in enumerate(pmers):
            if isinstance(pmer, PMap):
                pmer = pmer.checkpoint()
            elif isinstance(pmer, PJoin):
                pmer = pmer.reduce(lambda l, r: (list(l), list(r)))
            
            if i == 0:
                graph = pmer.pmer.graph
            else:
                graph = pmer.pmer.graph.union(graph)

            sources.append(pmer.source)

        name = kwargs.pop('name', 'polymr/{}'.format(random.random()))
        ds = pmer.pmer.runner(name, graph, **kwargs).run(sources)
        return [ValueEmitter(d) for d in ds]

    def _add_mapper(self, *args, **kwargs): 
        output, ng = self.graph.add_mapper(*args, **kwargs)
        return output, Polymr(ng)

    def _add_reducer(self, *args, **kwargs): 
        output, ng = self.graph.add_reducer(*args, **kwargs)
        return output, Polymr(ng)

    def _add_sink(self, *args, **kwargs): 
        output, ng = self.graph.add_sink(*args, **kwargs)
        return output, Polymr(ng)

def fuse(aggs):
    if len(aggs) == 1:
        return aggs[0].mapper

    def run(it, agg):
        return ((ki, vi) for k, v in it for ki, vi in agg.mapper(k, v))

    def _fuse(k, v):
        it = iter([(k, v)])
        for agg in aggs:
            it = run(it, agg)

        return it

    return _fuse

# This reinitializaes everytime
RANDOM = None
def get_rand():
    global RANDOM
    if RANDOM is None:
        RANDOM = random.Random(time.time())

    return RANDOM
