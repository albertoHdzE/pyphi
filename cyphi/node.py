#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from marbl import Marbl
import numpy as np
import functools


# TODO extend to nonbinary nodes
# TODO? refactor to use purely indexes for nodes
@functools.total_ordering
class Node(object):

    """A node in a network.

    Attributes:
        network (Network): The network the node belongs to.
        index (int): The node's index in the network's list of nodes.
        label (str): An optional label for the node.
        inputs (list(Node)): A list of nodes that have connections to this
            node.
        tpm (np.ndarray): The TPM for this node. ``this_node.tpm[0]`` and
            ``this_node.tpm[1]`` gives the probability tables that this node is
            off and on, respectively, indexed by network state, **after
            marginalizing-out nodes that don't connect to this node**.

    Examples:
        In a 3-node network, ``self.tpm[0][(0, 1, 0)]`` gives the probability
        that this node is off at |t_0| if the state of the network is |0,1,0|
        at |t_{-1}|.
    """

    # TODO document constructor args
    def __init__(self, network, index, connectivity_matrix=None, label=None):
        # This node's parent network.
        self.network = network
        # This node's index in the network's list of nodes.
        self.index = index
        # Label for display.
        self.label = label
        # Connectivity matrix to determine inputs and outputs.
        # This can be used to encode unidirectional cuts.
        # If none was given, default to the network's connectivity matrix.
        if connectivity_matrix is None:
            self.connectivity_matrix = self.network.connectivity_matrix
        else:
            self.connectivity_matrix = connectivity_matrix
        # Get indices of the inputs.
        if self.connectivity_matrix is not None:
            # If a connectivity matrix was provided, store the indices of nodes
            # that connect to this node.
            self._input_indices = np.array(
                [i for i in range(self.network.size) if
                 self.network.connectivity_matrix[i][self.index]])
            self._output_indices = np.array(
                [i for i in range(self.network.size) if
                 self.network.connectivity_matrix[self.index][i]])
        else:
            # If no connectivity matrix was provided, assume all nodes connect
            # to all nodes.
            self._input_indices = tuple(range(self.network.size))

        # This will hold the indices of the nodes that correspond to
        # non-singleton dimensions of this node's on-TPM. It maps any network
        # node index to the corresponding dimension of this node's TPM with
        # singleton dimensions removed. We need this for creating this node's
        # Marbl.
        self._dimension_labels = []
        # Generate the node's TPM.
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        tpm_on = self.network.tpm[..., self.index]
        tpm_off = 1 - tpm_on
        # Marginalize-out non-input nodes.
        current_non_singleton_dim_index = 0
        for index in range(self.network.size):
            if index not in self._input_indices:
                # Record that this node index doesn't correspond to any
                # dimension in this node's squeezed TPM.
                self._dimension_labels.append(None)
                # TODO extend to nonbinary nodes
                tpm_on = tpm_on.sum(index, keepdims=True) / 2
                tpm_off = tpm_off.sum(index, keepdims=True) / 2
            else:
                # The current index will correspond to a dimension in this
                # node's squeezed TPM, so we map it to the index of the
                # corresponding dimension and increment the corresponding index
                # for the next one.
                self._dimension_labels.append(current_non_singleton_dim_index)
                current_non_singleton_dim_index += 1
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # Store the generated TPM.
        self.tpm = np.array([tpm_off, tpm_on])
        # Make it immutable (for hashing).
        self.tpm.flags.writeable = False

        # Only compute hash once.
        self._hash = hash((self.network, self.index))

        # Deferred properties:
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # ``inputs``, ``outputs``, and ``marbl`` must be properties because at
        # the time of node creation, the network doesn't have a list of Node
        # objects yet, only a size (and thus a range of node indices). So, we
        # defer construction until the properties are needed.
        self._inputs = None
        self._outputs = None
        self._marbl = None
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    @property
    def inputs(self):
        """The set of nodes with connections to this node."""
        if self._inputs is not None:
            return self._inputs
        else:
            self._inputs = [node for node in self.network.nodes if node.index
                            in self._input_indices]
            return self._inputs

    @property
    def outputs(self):
        """The set of nodes this node has connections to."""
        if self._outputs is not None:
            return self._outputs
        else:
            self._outputs = set(node for node in self.network.nodes if
                                node.index in self._output_indices)
            return self._outputs

    @property
    def marbl(self):
        """The normalized representation of this node's Markov blanket."""
        if self._marbl is not None:
            return self._marbl
        else:
            # We take only the part of the TPM giving the probability the node
            # is on
            # TODO extend to nonbinary nodes
            augmented_child_tpms = [
                [child._dimension_labels[self.index], child.tpm[1].squeeze()]
                for child in self.outputs
            ]
            self._marbl = Marbl(self.tpm[1], augmented_child_tpms)
            return self._marbl

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return (self.label if self.label is not None
                else 'n' + str(self.index))

    def __eq__(self, other):
        """Return whether this node equals the other object.

        Two nodes are equal if they belong to the same network and have the
        same index (``tpm`` must be the same in that case, so this method
        doesn't need to check ``tpm`` equality).

        Labels are for display only, so two equal nodes may have different
        labels.
        """
        return ((self.index == other.index and self.network == other.network)
                if isinstance(other, type(self)) else False)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return self._hash

    def __lt__(self, other):
        return self.index < other.index
