"""
A parallel implementation of Smyth 1997's HMM clustering algorithm
for 1-dimensional time series data.
	Syntax: python smyth.py infile target_m min_k max_k outpath
Models for the time series in infile are built and pickled to outpath. target_m,
min_k, and max_k are parameters for the modeling process. See HMMCluster's
documentation for explanations.
@author: Julian Applebaum
"""

from ghmm import Float, GaussianDistribution, HMMFromMatrices, SequenceSet
from sklearn.cluster import k_means
from fastcluster import linkage
from Pycluster import kmedoids, treecluster
from scipy.cluster.hierarchy import fcluster
from scipy.spatial.distance import squareform
from numpy import std, mean, array, float32
from sample_gen import smyth_example
from cluster_utils import partition
from sequence_utils import *
from hmm_utils import compositeTriple, hmmToTriple, tripleToHMM
from matrix_utils import uniformMatrix
from levenshtein import levDistance
from pprint import pprint
from math import isnan
from multiprocessing import Pool
from itertools import izip, islice, ifilter
from time import clock
import sys, cPickle

# Pad out the Gaussian if we get a cluster with uniform data - o/w, we
# get undefined Gaussians on HMM initialization.
EPSILON = .5

def printAndFlush(string):
	"""
	Print a string and flush stdout.
	"""
	print string
	sys.stdout.flush()

# These functions really belong as methods of HMMCluster, but we need to leave
# them at the module level for multiprocessing.
def prepareSeqs(S):
	"""
	Combine the observations from a set of sequences into a merged list of
	1d vectors, and get the set of distinct observation values in one pass.
	@param S: the set of sequences
	@return: A pair (merged, distinct)
	"""
	distinct = set()
	merged = []
	for s in S:
		for o in s:
			merged.append([o])
			distinct.add(o)
	return (merged, distinct)

def smythEmissionDistribution(pair):
	"""
	Given a pair (S: list of sequences, target_m: int), get the emission
	distribution for Smyth's "default" HMM. target_m is an upper bound on the
	number of states -- if we can only have m' distinct observation values, then
	the distribution for a m' state HMM is returned.

	@param pair: A tuple of the form (S: list of sequences, m: int)
	@return: The corresponding emission distribution encoded as a list
		of (mu, stddev) pairs
	"""
	S, target_m = pair
	merged, distinct = prepareSeqs(S)
	m_prime = min(target_m, len(distinct))
	centroids, labels, inertia = k_means(merged, m_prime, init='k-means++')
	clusters = partition(merged, labels)
	B = []
	for cluster in clusters:
		assert len(cluster) > 0
		mu = mean(cluster)
		stddev = std(cluster)
		B.append((mu, stddev))
	return B

def smythDefaultTriple(pair):
	"""
	Given a pair (S: list of sequences, target_m: int), initialize a
	HMM triple with at most target_m states using Smyth's "default" method.
	If the observations in S can be clustered into target_m non-empty cluster,
	then the resulting model will have target_m states. Otherwise, the model
	will have one state per non-empty cluster for however many clusters could
	be created.

	@param pair: A tuple of the form (S: list of sequences, target_m: int)
	@return: The HMM as a (A, B, pi) triple
	"""
	cluster, target_m = pair
	B = smythEmissionDistribution(pair)
	m_prime = len(B)
	A = uniformMatrix(m_prime, m_prime, 1.0/m_prime)
	pi = [1.0/m_prime] * m_prime
	hmm = tripleToHMM((A, B, pi))
	hmm.baumWelch(toSequenceSet(cluster))
	A_p, B_p, pi_p = hmmToTriple(hmm)
	# According to the GHMM mailing list, a very small standard deviation can
	# cause underflow errors when attempting to compute log likelihood. We
	# avoid this by placing a floor sigma >= .5. It's a little hacky, but given
	# the very fuzzy nature of our training data (considering network latency,
	# etc.), it's not unreasonable to assume that "uniform" measurements really
	# should have some jitter.
	B_p = map(lambda b: (b[0], max(b[1], EPSILON)), B)
	return (A_p, B_p, pi_p)

def randomDefaultTriple(pair):
	pass

def symDistance(args):
	"""
	Calculate Rabiner's symmetrized distance measure between two sequences
	given their corresponding "default" models.

	@param args: A pair ((seq1, triple1), (seq2, triple2)) where seq1 and
		seq2 are singleton lists of emission sequences, and triple1, triple2
		are the corresponding HMM triples.
	@return: The distance between seq1 and seq2
	"""
	pair1, pair2 = args
	seq1, triple1 = pair1
	seq2, triple2 = pair2
	hmm1 = tripleToHMM(triple1)
	hmm2 = tripleToHMM(triple2)
	s1_m2 = hmm2.loglikelihood(toSequence(seq1))
	s2_m1 = hmm1.loglikelihood(toSequence(seq2))
	assert s1_m2 <= 0
	assert s2_m1 <= 0
	return (s1_m2 + s2_m1)/2.0

def reestimated(pair):
	"""
	Perform Baum-Welch reestimation on a HMM triple with the sequences in S.
	@param pair: a tuple (triple, S)
	@return: the reestimated triple
	"""
	triple, S = pair
	seqSet = toSequenceSet(S)
	hmm = tripleToHMM(triple)
	hmm.baumWelch(seqSet)
	return hmmToTriple(hmm)

def trainHMM(pair):
	"""
	Given a pair (m: int, S: list of sequences), train a HMM with at
	most m states on S. The HMM is initialized with Smyth's default method,
	then refined with Baum-Welch training.

	@param item: A tuple (m: int, S: list of sequences)
	@return: A triple (A, B, pi) representing the trained HMM
	"""
	cluster, m = pair
	return reestimated((smythDefaultTriple(pair), cluster))

def kMedoids(args):
	"""
	Do k-medoids clustering on a distance matrix.
	@param args: A tuple of the form (dist_matrix, k, n_passes)
	@return: The result tuple returned by Pycluster.kmedoids
	"""
	dist_matrix, k, n_passes = args
	return kmedoids(dist_matrix, k, n_passes)

class HMMCluster():
	def __init__(self, S, target_m, min_k, max_k, dist_func='hmm',
			hmm_init='smyth', clust_alg='hierarchical', train_mode='cluster',
			n_jobs=None):
		"""
		@param S: The sequences to model
		@param target_m: The desired number of components per HMM. The training
			algorithm will attempt to create this many states, but
			it is not guaranteed. See smythDefaultTriple for details.
		@param min_k: The minimum number of mixture components to try
		@param max_k: The maximum number of mixture components to try
		@param dist_func: The distance function to use; either 'hmm' or
			'editdistance'. 'hmm' is Rabiner's symmetrized measure.
		@param hmm_init: Either 'smyth' or 'random'. 'smyth' causes HMMs to
			be initialized with Smyth 1997's "default" method. 'random'
			results in random transition matrices, emission distributions
			and intial state distributions.
		@param clust_alg: Either 'hierarchical' or 'kmedoids'. Specifies
			which clustering algorithm to use.
		@param train_mode: Either 'blockdiag' or 'cluster'. If 'blockdiag',
			we make the block diagonal model and perform Baum-Welch with the
			whole dataset (the way Smyth does). If 'cluster', we train on each
			cluster, then combine into the block diagonal.
		@param n_jobs: How many processes to spawn for parallel computations.
			If None, cpu_count() processes are created.
		"""
		self.S = S
		self.n = len(self.S)
		self.target_m = target_m
		self.min_k = min_k
		self.max_k = max_k
		self.dist_func = dist_func
		self.hmm_init = hmm_init
		self.clust_alg = clust_alg
		self.train_mode = train_mode
		self._sanityCheck()
		self.components = {}
		self.composites = {}
		self.dist_matrix = None
		self.partitions = {}
		self.labelings = {}
		self.k_values = range(self.min_k, self.max_k+1)
		self.init_hmms = []
		self.pool = Pool(n_jobs)
		self.times = {}

	def _sanityCheck(self):
		assert self.min_k <= self.max_k
		assert self.dist_func in ('hmm', 'editdistance')
		assert self.hmm_init in ('smyth', 'random')
		assert self.clust_alg in ('hierarchical', 'kmedoids')
		assert self.train_mode in ('blockdiag', 'cluster')

	def _getHMMBatchItems(self):
		for i in xrange(0, self.n):
			for j in xrange(1+i, self.n):
				pair_1 = (self.S[i], self.init_hmms[i])
				pair_2 = (self.S[j], self.init_hmms[j])
				yield (pair_1, pair_2)

	def _getHMMDistMatrix(self):
		"""
		Compute the distance matrix using Rabiner's HMM distance measure.
		"""
		if self.hmm_init == 'smyth':
			init_fn = smythDefaultTriple
		elif self.hmm_init == 'random':
			init_fn = randomDefaultTriple
		printAndFlush("Generating default HMMs (parallel)...")
		start = clock()
		self.init_hmms = self.pool.map(init_fn,
			(([s], self.target_m) for s in self.S))
		self.times['init_hmms'] = clock() - start
		printAndFlush("done")
		n_batchitems = (self.n)*(self.n+1)/2 - self.n
		condensed = []
		printAndFlush("Computing distance matrix (parallel)...")
		printAndFlush("Processing %i batch items" % n_batchitems)
		start = clock()
		# Split the distance matrix calculation into mini batches of 500,000
		# pairs to avoid a bug in the multiprocessing API.
		batch_size = 500000
		for i in xrange(0, (n_batchitems)/batch_size + 1):
			start, stop = batch_size*i, min(batch_size*(i+1), n_batchitems)
			printAndFlush("Items %i-%i" % (start, stop))
			dist_batch = self._getHMMBatchItems()
			mini_batch = islice(dist_batch, start, stop)
			condensed += self.pool.map(symDistance, mini_batch)
		self.times['distance_matrix'] = clock() - start
		printAndFlush("done")
		# log-likelihoods are <= 0, a distance function must be positive
		shifted = map(lambda l: -1*l, condensed)
		printAndFlush(("Minimum distance:", min(shifted)))
		printAndFlush(("Maximum distance:", max(shifted)))
		return array(shifted, float32)

	def _getEditDistMatrix(self):
		"""
		Compute the distance matrix using edit distance between sequences.
		"""
		dist_batch = []
		for i in xrange(0, self.n):
			for j in xrange(1+i, self.n):
				dist_batch.append((self.S[i], self.S[j]))
		printAndFlush("Computing distance matrix (parallel)...")
		start = clock()
		condensed = self.pool.map(levDistance, dist_batch)
		self.times['distance_matrix'] = clock() - start
		printAndFlush("done")
		return condensed

	def _getDistMatrix(self):
		"""
		Compute the distance matrix with a user specified distance function.
		"""
		if self.dist_func == 'hmm':
			condensed = self._getHMMDistMatrix()
		elif self.dist_func == 'editdistance':
			condensed = self._getEditDistMatrix()
		return condensed

	def _hierarchical(self):
		"""
		Create multiple partitions for k values in [self.min_k... self.max_k]
		via hierarchical, agglomerative clustering.
		"""
		self.dist_matrix = self._getDistMatrix()
		printAndFlush("Hierarchical clustering (serial)...")
		tree = treecluster(distancematrix=self.dist_matrix, method='m')
		# linkage_matrix = linkage(self.dist_matrix, method='complete')
		for k in self.k_values:
			labels = tree.cut(k)
			# labels = fcluster(linkage_matrix, k, 'maxclust')
			self.labelings[k] = labels
			clusters = partition(self.S, labels)
			print len(clusters)
			self.partitions[k] = (clusters)
		printAndFlush("done")

	def _kMedoids(self):
		"""
		Create multiple partitions for k values in [self.min_k... self.max_k]
		via k-medoids.
		"""
		self.dist_matrix = self._getDistMatrix()
		batch_items = ((self.dist_matrix, k, 10) for k in self.k_values)
		printAndFlush("K-medoids clustering (parallel)...")
		results = self.pool.map(kMedoids, batch_items)
		printAndFlush("done")
		for i in xrange(0, len(self.k_values)):
			k, result = self.k_values[i], results[i]
			labels, error, nfound = result
			self.labelings[k] = labels
			clusters = partition(self.S, labels)
			self.partitions[k] = (clusters)

	def _cluster(self):
		"""
		Create multiple partitions for k values in [self.min_k... self.max_k]
		with a user specified clustering algorithm.
		"""
		start = clock()
		if self.clust_alg == 'hierarchical':
			self._hierarchical()
		elif self.clust_alg == 'kmedoids':
			self._kMedoids()
		self.times['clustering'] = clock() - start

	def _trainModels(self):
		"""
		Train a HMM mixture on each of the k-partitions by separately training
		an HMM on each cluster.
		"""
		batch_items = []
		cluster_sizes = []
		seq_lens = []
		# Build a list of mapping items to submit as a bulk job
		for k in self.k_values:
			partition = self.partitions[k]
			for cluster in partition:
				cluster_sizes.append(len(cluster))
				seq_lens.append(map(lambda s: len(s), cluster))
				batch_items.append((cluster, self.target_m))
		for k in self.k_values:
			self.components[k] = {
				'hmm_triples': [],
				'cluster_sizes': [],
				'seq_lens': []
			}
		printAndFlush("Training components on clusters (parallel)...")
		start = clock()
		hmm_triples = self.pool.map(trainHMM, batch_items)
		self.times['modeling'] = clock() - start
		printAndFlush("done")
		idx = 0
		# Reconstruct the mixtures for each k from the list of trained HMMS
		for k in self.k_values:
			for i in xrange(0, k):
				cluster_size = cluster_sizes[idx]
				inclust_seq_lens = seq_lens[idx]
				hmm_triple = hmm_triples[idx]
				self.components[k]['hmm_triples'].append(hmm_triple)
				self.components[k]['cluster_sizes'].append(cluster_size)
				self.components[k]['seq_lens'].append(inclust_seq_lens)
				idx += 1
			composite = tripleToHMM(compositeTriple(self.components[k]))
			self.composites[k] = hmmToTriple(composite)
		print "done"

	def model(self):
		"""
		With the user specified k range, clustering algorithm, HMM intialization,
		and distance function, create a set of HMM mixtures modeling the
		sequences in self.S. When finished, self.components is populated with a
		dict mapping k values to HMM triples.
		"""
		start = clock()
		self._cluster()
		self._trainModels()
		self.times['total'] = clock() - start
		self.pool.close()

if __name__ == "__main__":
	print "Generating synthetic data...",
	seqSet = smyth_example(n=40, length=200, seed=11)
	print "done"
	clust = HMMCluster(seqSetToList(seqSet), 2, 2, 2,
		clust_alg='hierarchical')
	clust.model()
	hmm = tripleToHMM(compositeTriple(clust.components[2]))
	hmm.baumWelch(seqSet)
	print hmm
