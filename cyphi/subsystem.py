#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Subsystem
~~~~~~~~~

Represents a candidate set for |phi| calculation.
"""

import numpy as np
from itertools import chain
from .constants import DIRECTIONS, PAST, FUTURE, MAXMEM
from .lru_cache import lru_cache
from . import options, validate
# TODO use namespaces more (honking great idea, etc.)
from . import utils
from .utils import hamming_emd, max_entropy_distribution, bipartition
from .node import Node
from .models import Mechanism, Cut, Mip, Part, Concept


# TODO! go through docs and make sure to say when things can be None
# TODO! make a NodeList object; factor out all_connect_to_any and any other
# methods that are really properties of lists of nodes
# TODO! document subsystem
class Subsystem:

    """A set of nodes in a network."""

    def __init__(self, node_indices, current_state, past_state, network,
                 cut=None):
        """
        Args:
            node_indices (tuple(int)): A sequence of indices of the nodes in
                this subsystem.
            current_state (tuple): The current state of this subsystem.
            past_state (tuple): The past state of this subsystem.
            network (Network): The network the subsystem is part of.
        """
        # Remove duplicates, sort, and cast to tuple for hashing
        node_indices = tuple(sorted(list(set(node_indices))))
        self.node_indices = node_indices

        # The null cut (leaves the system intact).
        self.null_cut = Cut(severed=(), intact=self.node_indices)

        # Get or generate the nodes in this subsystem.
        if cut is None:
            self.nodes = tuple(network.nodes[i] for i in node_indices)
            self.connectivity_matrix = network.connectivity_matrix
            self.cut = self.null_cut
        else:
            # Cast to cut in case we got a plain tuple
            self.cut = Cut(cut[0], cut[1])
            # Create new nodes with the cut applied to the connectivity matrix,
            # so their TPMs encode the cut
            cm = utils.apply_cut(self.cut, network.connectivity_matrix)
            self.nodes = tuple(Node(network, i, cm) for i in node_indices)
            self.connectivity_matrix = cm

        self.current_state = current_state
        self.past_state = past_state

        # The network this subsystem belongs to.
        self.network = network

        # A cache for keeping core causes and effects that can be reused later
        self._mice_cache = dict()

        # Hash doesn't include cut
        self._hash = hash((self.node_indices,
                           self.current_state,
                           self.past_state,
                           self.network))

    def __repr__(self):
        return "Subsystem(" + ", ".join([repr(self.nodes),
                                         repr(self.current_state),
                                         repr(self.past_state)]) + ")"

    def __str__(self):
        return repr(self)

    def __eq__(self, other):
        """Return whether this subsystem is equal to the other object.

        Two subsystems are equal if their sets of nodes, current and past
        states, and networks are equal."""
        return ((set(self.nodes) == set(other.nodes) and
                 self.current_state == other.current_state and
                 self.past_state == other.past_state and
                 self.network == other.network)
                if isinstance(other, type(self)) else False)

    def __bool__(self):
        """Return false if the subsystem has no nodes, true otherwise."""
        return bool(self.nodes)

    def __ne__(self, other):
        return not self.__eq__(other)

    # TODO write tests for cmp methods
    def __ge__(self, other):
        return len(self.nodes) >= len(other.nodes)

    def __le__(self, other):
        return len(self.nodes) <= len(other.nodes)

    def __gt__(self, other):
        return len(self.nodes) > len(other.nodes)

    def __lt__(self, other):
        return len(self.nodes) < len(other.nodes)

    def __hash__(self):
        return self._hash

    @lru_cache(maxmem=MAXMEM)
    def cause_repertoire(self, mechanism, purview):
        """Return the cause repertoire of a mechanism over a purview.

        Args:
            mechanism (tuple(Node)): The mechanism for which to calculate the
                cause repertoire.
            purview (tuple(Node)): The purview over which to calculate the
                cause repertoire.

        Returns:
            An array representing the cause repertoire of the
            mechanism over a purview.
        """
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # NOTE: In the Matlab version's terminology,
        #
        # "Cause repertoire" is "backward repertoire"
        # "Mechanism" is "numerator"
        # "Purview" is "denominator"
        # ``conditioned_tpm`` is ``next_num_node_distribution``
        # ``cjd`` is ``numerator_conditional_joint``
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        mechanism = validate.nodelist(mechanism, 'Mechanism')
        purview = validate.nodelist(purview, 'Purview')
        # If the mechanism is empty, nothing is specified about the past state
        # of the purview, so just return the purview's maximum entropy
        # distribution.
        if not mechanism:
            return max_entropy_distribution(purview, self.network)
        # If the purview is empty, the distribution is empty, so return the
        # multiplicative identity.
        if not purview:
            return np.array([1])
        # Preallocate the mechanism's conditional joint distribution.
        # TODO extend to nonbinary nodes
        cjd = np.ones(tuple(2 if node in purview else
                            1 for node in self.network.nodes))
        # Loop over all nodes in this mechanism, successively taking the
        # product (with expansion/broadcasting of singleton dimensions) of each
        # individual node's TPM (conditioned on that node's state) in order to
        # get the conditional joint distribution for the whole mechanism
        # (conditioned on the whole mechanism's state). After normalization,
        # this is the cause repertoire. Normalization happens after this loop.
        for mechanism_node in mechanism:
            inputs = set(mechanism_node.inputs)

            # TODO extend to nonbinary nodes
            # We're conditioning on this node's state, so take the probability
            # table for the node being in that state.
            node_state = self.current_state[mechanism_node.index]
            conditioned_tpm = mechanism_node.tpm[node_state]
            # Collect the nodes that are not in the purview and have
            # connections to this node.
            non_purview_inputs = (inputs &
                                  (set(self.network.nodes) - set(purview)))
            # Fixed boundary-condition nodes are those that are outside this
            # subsystem, and are either not in the purview or have been severed
            # by a cut.
            boundary_inputs = non_purview_inputs - set(self.nodes)
            # We will marginalize-out nodes that are within the subsystem, but
            # are either not in the purview or severed by a cut.
            marginal_inputs = non_purview_inputs - boundary_inputs
            # Condition the CPT on the past states of the nodes that are
            # treated as fixed boundary conditions by collapsing the dimensions
            # corresponding to the fixed nodes' indices so they contain only
            # the probabilities that correspond to their past states.
            for node in boundary_inputs:
                conditioning_indices = [slice(None)] * self.network.size
                conditioning_indices[node.index] = \
                    [self.past_state[node.index]]
                conditioned_tpm = conditioned_tpm[conditioning_indices]
            # Marginalize-out the nodes in this subsystem with inputs to this
            # mechanism that are either not in the purview or whose connections
            # to this mechanism have not been severed by a subsystem cut.
            for node in marginal_inputs:
                conditioned_tpm = (conditioned_tpm.sum(node.index,
                                                       keepdims=True)
                                   / conditioned_tpm.shape[node.index])
            # Incorporate this node's CPT into the mechanism's conditional
            # joint distribution by taking the product (with singleton
            # broadcasting, which spreads the singleton probabilities in the
            # collapsed dimensions out along the whole distribution in the
            # appropriate way.
            cjd *= conditioned_tpm
        # Finally, normalize to get the mechanism's actual conditional joint
        # ditribution.
        cjd_sum = np.sum(cjd)
        # Don't divide by zero
        if cjd_sum != 0:
            cjd /= cjd_sum
        # NOTE: we're not returning a distribution over all the nodes in the
        # network, only a distribution over the nodes in the purview. This is
        # because we never actually need to compare proper cause/effect
        # repertoires, which are distributions over the whole network; we need
        # only compare the purview-repertoires with each other, since cut vs.
        # whole comparisons are only ever done over the same purview.
        return cjd

    @lru_cache(maxmem=MAXMEM)
    def effect_repertoire(self, mechanism, purview):
        """Return the effect repertoire of a mechanism over a purview.

        mechanism (tuple(Node)): The mechanism for which to calculate the
            effect repertoire.
        purview (tuple(Node)): The purview over which to calculate the effect
            repertoire.

        Returns:
            An np.array representing the effect repertoire of the mechanism
            over a purview.
        """
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # NOTE: In the Matlab version's terminology,
        #
        # "Effect repertoire" is "forward repertoire"
        # "Mechanism" is "numerator"
        # "Purview" is "denominator"
        # ``conditioned_tpm`` is ``next_denom_node_distribution``
        # ``accumulated_cjd`` is ``denom_conditional_joint``
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        mechanism = validate.nodelist(mechanism, 'Mechanism')
        purview = validate.nodelist(purview, 'Purview')
        # If the purview is empty, the distribution is empty, so return the
        # multiplicative identity.
        if not purview:
            return np.array([1])
        # Preallocate the purview's joint distribution
        # TODO extend to nonbinary nodes
        accumulated_cjd = np.ones(
            [1] * self.network.size + [2 if node in purview else 1 for node in
                                       self.network.nodes])
        # Loop over all nodes in the purview, successively taking the product
        # (with 'expansion'/'broadcasting' of singleton dimensions) of each
        # individual node's TPM in order to get the joint distribution for the
        # whole purview. After conditioning on the mechanism's state and that
        # of external nodes, this will be the effect repertoire as a
        # distribution over the purview.
        for purview_node in purview:
            # Unlike in calculating the cause repertoire, here the TPM is not
            # conditioned yet. `tpm` is an array with twice as many dimensions
            # as the network has nodes. For example, in a network with three
            # nodes {n0, n1, n2}, the CPT for node n1 would have shape
            # (2,2,2,1,2,1). The CPT for the node being off would be given by
            # `tpm[:,:,:,0,0,0]`, and the CPT for the node being on would be
            # given by `tpm[:,:,:,0,1,0]`. The second half of the shape is for
            # indexing based on the current node's state, and the first half of
            # the shape is the CPT indexed by network state, so that the
            # overall CPT can be broadcast over the `accumulated_cjd` and then
            # later conditioned by indexing.

            inputs = set(purview_node.inputs)
            # TODO extend to nonbinary nodes
            # Rotate the dimensions so the first dimension is the last
            tpm = purview_node.tpm
            tpm = tpm.transpose(list(range(tpm.ndim))[1:] + [0])
            # Expand the dimensions so the TPM can be indexed as described
            first_half_shape = list(tpm.shape[:-1])
            second_half_shape = [1] * self.network.size
            second_half_shape[purview_node.index] = 2
            tpm = tpm.reshape(first_half_shape + second_half_shape)

            # We marginalize-out inputs to the current purview node that are
            # within the subsystem but not in the mechanism, or those that were
            # severed by a cut.
            marginal_inputs = inputs & (set(self.nodes) - set(mechanism))
            for node in marginal_inputs:
                tpm = (tpm.sum(node.index, keepdims=True)
                       / tpm.shape[node.index])
            # Incorporate this node's CPT into the future_nodes' conditional
            # joint distribution by taking the product (with singleton
            # broadcasting).
            accumulated_cjd = accumulated_cjd * tpm

        # Now we condition on the state of the boundary nodes, whose states we
        # fix (by collapsing the CJD onto those states):
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # Collect all nodes with inputs to any purview node.
        inputs_to_purview = set.union(*[set(node.inputs) for node in purview])
        # Collect nodes outside this subsystem.
        external_nodes = set(self.network.nodes) - set(self.nodes)
        # Fixed boundary condition nodes are those that are outside this
        # subsystem or in the mechanism, and have been severed by a
        # cut.
        boundary_inputs = inputs_to_purview & (set(mechanism) | external_nodes)
        # Initialize the conditioning indices, taking the slices as singleton
        # lists-of-lists for later flattening with `chain`.
        # TODO! are the external nodes really the ones outside this
        # subsystem?
        conditioning_indices = [[slice(None)]] * self.network.size
        for node in boundary_inputs:
            # Preserve singleton dimensions with `np.newaxis`
            conditioning_indices[node.index] = [self.current_state[node.index],
                                                np.newaxis]
        # Flatten the indices
        conditioning_indices = list(chain.from_iterable(conditioning_indices))
        # Obtain the actual conditioned distribution by indexing with the
        # conditioning indices
        accumulated_cjd = accumulated_cjd[conditioning_indices]
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

        # The distribution still has twice as many dimensions as the network
        # has nodes, with the first half of the shape now all singleton
        # dimensions, so we reshape to eliminate those singleton dimensions
        # (the second half of the shape may also contain singleton dimensions,
        # depending on how many nodes are in the purview).
        accumulated_cjd = accumulated_cjd.reshape(
            accumulated_cjd.shape[self.network.size:2 * self.network.size])

        # Note that we're not returning a distribution over all the nodes in
        # the network, only a distribution over the nodes in the purview. This
        # is because we never actually need to compare proper cause/effect
        # repertoires, which are distributions over the whole network; we need
        # only compare the purview-repertoires with each other, since cut vs.
        # whole comparisons are only ever done over the same purview.
        return accumulated_cjd

    # TODO check if the cache is faster
    def _get_repertoire(self, direction):
        """Returns the cause or effect repertoire function based on a
        direction.

        Args:
            direction (str): The temporal direction, specifiying the cause or
                effect repertoire.

        Returns:
            The cause or effect repertoire function.
        """
        if direction == DIRECTIONS[PAST]:
            return self.cause_repertoire
        elif direction == DIRECTIONS[FUTURE]:
            return self.effect_repertoire

    def _unconstrained_repertoire(self, direction, purview):
        """Return the unconstrained cause or effect repertoire over a
        purview."""
        return self._get_repertoire(direction)((), purview)

    # TODO! move exposed API functions that aren't interally used
    def unconstrained_cause_repertoire(self, purview):
        """Return the unconstrained cause repertoire for a purview.

        This is just the cause repertoire in the absence of any mechanism.
        """
        return self._unconstrained_repertoire(DIRECTIONS[PAST], purview)

    def unconstrained_effect_repertoire(self, purview):
        """Return the unconstrained effect repertoire for a purview.

        This is just the effect repertoire in the absence of any mechanism.
        """
        return self._unconstrained_repertoire(DIRECTIONS[FUTURE], purview)

    def _expand_repertoire(self, direction, mechanism, purview, repertoire):
        """Return the unconstrained cause or effect repertoire based on a
        direction."""
        validate.direction(direction)
        non_purview_nodes = frozenset(self.nodes) - frozenset(purview)
        return (repertoire * self._unconstrained_repertoire(direction,
                                                            non_purview_nodes))

    # TODO test expand cause repertoire
    def expand_cause_repertoire(self, mechanism, purview, repertoire):
        """Expand a partial cause repertoire over a purview to a distribution
        over the entire subsystem's state space."""
        return self._expand_repertoire(DIRECTIONS[PAST], mechanism, purview,
                                       repertoire)

    # TODO test expand effect repertoire
    def expand_effect_repertoire(self, mechanism, purview, repertoire):
        """Expand a partial effect repertoire over a purview to a distribution
        over the entire subsystem's state space."""
        return self._expand_repertoire(DIRECTIONS[FUTURE], mechanism, purview,
                                       repertoire)

    def cause_info(self, mechanism, purview):
        """Return the cause information for a mechanism over a purview."""
        return hamming_emd(self.cause_repertoire(mechanism, purview),
                           self.unconstrained_cause_repertoire(purview))

    def effect_info(self, mechanism, purview):
        """Return the effect information for a mechanism over a purview."""
        return hamming_emd(self.effect_repertoire(mechanism, purview),
                           self.unconstrained_effect_repertoire(purview))

    def cause_effect_info(self, mechanism, purview):
        """Return the cause-effect information for a mechanism over a
        purview.

        This is the minimum of the cause and effect information."""
        return min(self.cause_info(mechanism, purview),
                   self.effect_info(mechanism, purview))

    # MIP methods
    # =========================================================================

    # TODO? something clever here so we don't do the full iteration
    @staticmethod
    def _mip_bipartition(mechanism, purview):
        purview_bipartitions = bipartition(purview)
        result = []
        for denominators in (purview_bipartitions +
                             list(map(lambda x: x[::-1],
                                      purview_bipartitions))):
            for numerators in bipartition(mechanism):
                # For the MIP, we only consider the bipartitions in which each
                # node appears exactly once, e.g. for AB/ABC, (A/B) * (C/[]) is
                # valid but (AB/BC) * ([]/A) is not (since B appears in both
                # numerator and denominator), and exclude partitions whose
                # numerator and denominator are both empty.
                valid_partition = (
                    len(numerators[0]) + len(denominators[0]) > 0 and
                    len(numerators[1]) + len(denominators[1]) > 0)
                if valid_partition:
                    part0 = Part(mechanism=numerators[0],
                                 purview=denominators[0])
                    part1 = Part(mechanism=numerators[1],
                                 purview=denominators[1])
                    result.append((part0, part1))
        return result

    @staticmethod
    def _null_mip(direction, mechanism, purview):
        # TODO Use properties here to infer mechanism and purview from
        # partition yet access them with .mechanism and .partition
        return Mip(direction=direction,
                   mechanism=mechanism,
                   purview=purview,
                   partition=None,
                   unpartitioned_repertoire=None,
                   partitioned_repertoire=None,
                   phi=0.0)

    def find_mip(self, direction, mechanism, purview):
        """Return the minimum information partition for a mechanism over a
        purview.

        Args:
            direction (str): Either |past| or |future|.
            mechanism (tuple(Node)): The nodes in the mechanism.
            purview (tuple(Node)): The nodes in the purview.

        Returns:
            :class:`cyphi.models.Mip`
        """
        validate.direction(direction)
        repertoire = self._get_repertoire(direction)

        # We default to the null MIP (the MIP of a reducible mechanism)
        mip = self._null_mip(direction, mechanism, purview)

        phi_min = float('inf')
        # Calculate the unpartitioned repertoire to compare against the
        # partitioned ones
        unpartitioned_repertoire = repertoire(mechanism, purview)

        # Loop over possible MIP bipartitions
        for part0, part1 in self._mip_bipartition(mechanism, purview):
            # Find the distance between the unpartitioned repertoire and
            # the product of the repertoires of the two parts, e.g.
            #   D( p(ABC/ABC) || p(AC/C) * p(B/AB) )
            part1rep = repertoire(part0.mechanism, part0.purview)
            part2rep = repertoire(part1.mechanism, part1.purview)
            partitioned_repertoire = part1rep * part2rep

            phi = hamming_emd(unpartitioned_repertoire, partitioned_repertoire)

            # Return immediately if mechanism is reducible
            if phi < options.EPSILON:
                return None
            # Update MIP if it's more minimal
            if (phi_min - phi) > options.EPSILON:
                phi_min = phi
                # TODO Use properties here to infer mechanism and purview from
                # partition yet access them with .mechanism and .partition
                mip = Mip(direction=direction,
                          mechanism=mechanism,
                          purview=purview,
                          partition=(part0, part1),
                          unpartitioned_repertoire=unpartitioned_repertoire,
                          partitioned_repertoire=partitioned_repertoire,
                          phi=phi)

        return mip

    # TODO Don't use these internally
    def mip_past(self, mechanism, purview):
        """Return the past minimum information partition.

        Alias for :func:`find_mip` with ``direction`` set to |past|.
        """
        return self.find_mip(DIRECTIONS[PAST], mechanism, purview)

    def mip_future(self, mechanism, purview):
        """Return the future minimum information partition.

        Alias for :func:`find_mip` with ``direction`` set to |future|.
        """
        return self.find_mip(DIRECTIONS[FUTURE], mechanism, purview)

    def phi_mip_past(self, mechanism, purview):
        """Return the |phi| value of the past minimum information partition.

        This is the distance between the unpartitioned cause repertoire and the
        MIP cause repertoire.
        """
        mip = self.mip_past(mechanism, purview)
        if mip:
            return mip.phi
        else:
            return 0

    def phi_mip_future(self, mechanism, purview):
        """Return the |phi| value of the future minimum information partition.

        This is the distance between the unpartitioned effect repertoire and
        the MIP cause repertoire.
        """
        mip = self.mip_future(mechanism, purview)
        if mip:
            return mip.phi
        else:
            return 0

    def phi(self, mechanism, purview):
        """Return the |phi| value of a mechanism over a purview."""
        return min(self.phi_mip_past(mechanism, purview),
                   self.phi_mip_future(mechanism, purview))

    # Phi_max methods
    # =========================================================================

    # TODO test phi max helpers
    @lru_cache(maxmem=MAXMEM)
    def _test_connections(self, axis, nodes1, nodes2):
        """Tests connectivity of one set of nodes to another.

        Args:
            axis (int): The axis over which to take the sum of the connectivity
                submatrix. If this is 0, the sum will be taken over the
                columns; in this case returning ``True`` means "all nodes in
                the second list have an input from some node in the first
                list". If this is 1, the sum will be taken over the rows, and
                returning ``True`` means "all nodes in the first list have a
                connection to some node in the second list".
            nodes1 ([Node]): the nodes whose outputs to ``nodes2`` will be
                tested
            nodes2 ([Node]): the nodes whose inputs from ``nodes1`` will
                be tested
        """
        if (self.network.connectivity_matrix is None or
                not nodes1 or not nodes2):
            return True
        # Get the connectivity matrix representing the connections from the
        # first node list to the second
        submatrix_indices = np.ix_([node.index for node in nodes1],
                                   [node.index for node in nodes2])
        cm = self.network.connectivity_matrix[submatrix_indices]
        # Check that all nodes have at least one connection by summing over
        # rows of connectivity submatrix
        return cm.sum(axis).all()

    # TODO test
    def _any_connect_to_all(self, nodes1, nodes2):
        """Return whether all nodes in the second list have inputs from some
        node in the first list."""
        return self._test_connections(0, nodes1, nodes2)

    # TODO test
    def _all_connect_to_any(self, nodes1, nodes2):
        """Return whether all nodes in the first list connect to some node in
        the second list."""
        return self._test_connections(1, nodes1, nodes2)

    # Big Phi methods
    # =========================================================================

    # TODO add `concept-space` section to the docs:
        # The first dimension corresponds to the direction, past or future; the
        # correspond to the subsystem's state space."""
    # TODO! make this a property
    def null_concept(self):
        """Return the null concept of this subsystem, a point in concept space
        identified with the unconstrained cause and effect repertoire of this
        subsystem.

        For information on the indices used in the returned array, see
        :ref:concept-space."""
        return Concept(
            mechanism=Mechanism(),
            location=np.array([
                # Unconstrained cause repertoire
                self.cause_repertoire((), self.nodes),
                # Unconstrained effect repertoire
                self.effect_repertoire((), self.nodes)
            ]),
            phi=0,
            cause=None,
            effect=None)
