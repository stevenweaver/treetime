from __future__ import print_function, division, absolute_import
import time, sys
from collections import defaultdict
import numpy as np
from Bio import Phylo
from Bio import AlignIO
from treetime import config as ttconf
from .seq_utils import seq2prof,seq2array,prof2seq
from .gtr import GTR

string_types = [str] if sys.version_info[0]==3 else [str, unicode]

class TreeAnc(object):
    """
    Class defines simple tree object with basic interface methods: reading and
    saving from/to files, initializing leaves with sequences from the
    alignment, making ancestral state inferrence
    """

    def __init__(self, tree=None, aln=None, gtr=None, fill_overhangs=True,
                ref=None, verbose = ttconf.VERBOSE, ignore_gaps=True,
                convert_upper=True, seq_multiplicity=None, log=None, **kwargs):
        """
        TreeAnc constructor. It prepares the tree, attaches sequences to the leaf nodes,
        and sets some configuration parameters.

        Parameters
        ----------

         tree : str, Bio.Phylo.Tree
            Phylogenetic tree. String passed is interpreted as a filename with
            a tree in a standard format that can be parsed by the Biopython Phylo module.

         aln : str, Bio.Align.MultipleSequenceAlignment, dict
            Sequence alignment. If a string passed, it is interpreted as the
            filename to read Biopython alignment from. If a dict is given,
            this is assumed to be the output of vcf_utils.read_vcf which
            specifies for each sequence the differences from a reference

         gtr : str, GTR
            GTR model object. If string passed, it is interpreted as the type of
            the GTR model. A new GTR instance will be created for this type.

         fill_overhangs : bool
            In some cases, the missing data on both ends of the alignment is
            filled with the gap sign('-'). If set to True, the end-gaps are converted to "unknown"
            characters ('N' for nucleotides, 'X' for aminoacids). Otherwise, the alignment is treated as-is

         ignore_gaps: bool
            Ignore gaps in branch length calculations

         verbose : int
            Verbosity level as number from 0 (lowest) to 10 (highest).

         seq_multiplicity: dict
            If individual nodes in the tree correspond to multiple sampled sequences
            (i.e. read count in a deep sequencing experiment), these can be
            specified as a dictionary. This currently only affects rooting and
            can be used to weigh individual tips by abundance or important during root search.

         **kwargs
            Keyword arguments to construct the GTR model

          .. Note::
                Some GTR types require additional configuration parameters.
                If the new GTR is being instantiated, these parameters are expected
                to be passed as kwargs. If nothing is passed, the default values are
                used, which might cause unexpected results.

        Raises
        ----------

         TypeError
            If a tree is not passed in


        """
        if tree is None:
            raise TypeError("TreeAnc requires a tree!")
        self.t_start = time.time()
        self.verbose = verbose
        self.log=log
        self.logger("TreeAnc: set-up",1)
        self._internal_node_count = 0
        self.use_mutation_length=False
        # if not specified, this will be set as 1/alignment_length
        self._seq_len = None
        self.seq_len = kwargs['seq_len'] if 'seq_len' in kwargs else None
        self.fill_overhangs = fill_overhangs
        self.is_vcf = False  #this is set true when aln is set, if aln is dict
        self.seq_multiplicity = {} if seq_multiplicity is None else seq_multiplicity
        self.multiplicity = None

        self.ignore_gaps = ignore_gaps
        self._gtr = None
        self.set_gtr(gtr or 'JC69', **kwargs)

        self._tree = None
        self.tree = tree
        if tree is None:
            raise AttributeError("TreeAnc: tree loading failed! exiting")

        # will be None if not set
        self.ref = ref

        # force all sequences to be upper case letters
        # (desired for nuc or aa, not for other discrete states)
        self.convert_upper = convert_upper

        # set alignment and attach sequences to tree on success.
        # otherwise self.aln will be None
        self._aln = None
        self.reduced_to_full_sequence_map = None
        self.aln = aln
        if self.aln and self.tree:
            if len(self.tree.get_terminals()) != len(self.aln):
                print("**WARNING: Number of sequences in tree differs from number of sequences in alignment!**")


    def logger(self, msg, level, warn=False):
        """
        Print log message *msg* to stdout.

        Parameters
        -----------

         msg : str
            String to print on the screen

         level : int
            Log-level. Only the messages with a level higher than the
            current verbose level will be shown.

         warn : bool
            Warning flag. If True, the message will be displayed
            regardless of its log-level.

        """
        if level<self.verbose or (warn and level<=self.verbose):
            dt = time.time() - self.t_start
            outstr = '\n' if level<2 else ''
            outstr += format(dt, '4.2f')+'\t'
            outstr += level*'-'
            outstr += msg
            print(outstr, file=sys.stdout)


####################################################################
## SET-UP
####################################################################
    @property
    def leaves_lookup(self):
        """
        The :code:`{leaf-name:leaf-node}` dictionary. It enables fast
        search of a tree leaf object by its name.
        """
        return self._leaves_lookup

    @property
    def gtr(self):
        """
        The current GTR object.

        :setter: Sets the GTR object passed in
        :getter: Returns the current GTR object

        """
        return self._gtr

    @gtr.setter
    def gtr(self, value):
        """
        Set a new GTR object

        Parameters
        -----------

         value : GTR
            the new GTR object
        """
        if not isinstance(value, GTR):
            raise TypeError(" GTR instance expected")
        self._gtr = value


    def set_gtr(self, in_gtr, **kwargs):
        """
        Create new GTR model if needed, and set the model as an attribute of the
        TreeAnc class

        Parameters
        -----------

         in_gtr : str, GTR
            The gtr model to be assigned. If string is passed,
            it is taken as the name of a standard GTR model, and is
            attempted to be created through :code:`GTR.standard()` interface. If a
            GTR instance is passed, it is set directly .

         **kwargs
            Keyword arguments to construct the GTR model. If none are passed, defaults
            are assumed.

        """
        if isinstance(in_gtr, str):
            self._gtr = GTR.standard(model=in_gtr, **kwargs)
            self._gtr.logger = self.logger

        elif isinstance(in_gtr, GTR):
            self._gtr = in_gtr
            self._gtr.logger=self.logger
        else:
            self.logger("TreeAnc.gtr_setter: can't interpret GTR model", 1, warn=True)
            raise TypeError("Cannot set GTR model in TreeAnc class: GTR or "
                "string expected")

        if self._gtr.ambiguous is None:
            self.fill_overhangs=False


    @property
    def tree(self):
        """
        The phylogenetic tree currently used by the TreeAnc.

        :setter: Sets the tree. Directly if passed as Phylo.Tree, or by reading from \
        file if passed as a str.
        :getter: Returns the tree as a Phylo.Tree object

        """
        return self._tree

    @tree.setter
    def tree(self, in_tree):
        '''
        assigns a tree to the internal self._tree variable. The tree is either
        loaded from file (if in_tree is str) or assigned (if in_tree is a Phylo.tree)
        '''

        from os.path import isfile
        if isinstance(in_tree, Phylo.BaseTree.Tree):
            self._tree = in_tree
        elif type(in_tree) in string_types and isfile(in_tree):
            try:
                self._tree=Phylo.read(in_tree, 'newick')
            except:
                fmt = in_tree.split('.')[-1]
                if fmt in ['nexus', 'nex']:
                    self._tree=Phylo.read(in_tree, 'nexus')
                else:
                    self.logger('TreeAnc: could not load tree, format needs to be nexus or newick! input was '+str(in_tree),1)
                    self._tree = None
                    return ttconf.ERROR
        else:
            self.logger('TreeAnc: could not load tree! input was '+str(in_tree),0)
            self._tree = None
            return ttconf.ERROR

        # remove all existing sequence attributes
        for node in self._tree.find_clades():
            if hasattr(node, "sequence"):
                node.__delattr__("sequence")
            node.original_length = node.branch_length
            node.mutation_length = node.branch_length
        self.prepare_tree()
        return ttconf.SUCCESS


    @property
    def aln(self):
        """
        The multiple sequence alignment currently used by the TreeAnc

        :setter: Takes in alignment as MultipleSeqAlignment, str, or dict/defaultdict \
        and attaches sequences to tree nodes.
        :getter: Returns alignment as MultipleSeqAlignment or dict/defaultdict

        """
        return self._aln

    @aln.setter
    def aln(self,in_aln):
        """
        Reads in the alignment (from a dict, MultipleSeqAlignment, or file,
        as necessary), sets tree-related parameters, and attaches sequences
        to the tree nodes.

        Parameters
        ----------
        in_aln : MultipleSeqAlignment, str, dict/defaultdict
            The alignment to be read in

        """
        # load alignment from file if necessary
        from os.path import isfile
        from Bio.Align import MultipleSeqAlignment
        self._aln = None
        if in_aln is None:
            return
        elif isinstance(in_aln, MultipleSeqAlignment):
            self._aln = in_aln
        elif type(in_aln) in string_types and isfile(in_aln):
            for fmt in ['fasta', 'phylip-relaxed', 'nexus']:
                try:
                    self._aln=AlignIO.read(in_aln, fmt)
                    break
                except:
                    continue
        elif type(in_aln) in [defaultdict, dict]:  #if is read in from VCF file
            self._aln = in_aln
            self.is_vcf = True

        if self._aln is None:
            self.logger("TreeAnc: loading alignment failed... ",1, warn=True)
            return ttconf.ERROR

        #Convert to uppercase here, rather than in _attach_sequences_to_nodes
        #(which used to do it through seq2array in seq_utils.py)
        #so that it is controlled by param convert_upper. This way for
        #mugration (ancestral reconstruction of non-sequences), you can
        #use upper- and lower case characters for discrete states!
        if (not self.is_vcf) and self.convert_upper:
            self._aln = MultipleSeqAlignment([seq.upper() for seq in self._aln])

        if self.is_vcf:
            self.seq_len = len(self.ref)
        else:
            self.seq_len = self.aln.get_alignment_length()

        if hasattr(self, '_tree') and (self.tree is not None):
            self._attach_sequences_to_nodes()
        else:
            self.logger("TreeAnc.aln: sequences not yet attached to tree", 3, warn=True)


    @property
    def seq_len(self):
        """length of the uncompressed sequence
        """
        return self._seq_len

    @seq_len.setter
    def seq_len(self,L):
        """set the length of the uncompressed sequence. its inverse 'one_mutation'
        is frequently used as a general length scale. This can't be changed once
        it is set.

        Parameters
        ----------
        L : int
            length of the sequence alignment
        """
        if (not hasattr(self, '_seq_len')) or self._seq_len is None:
            if L:
                self._seq_len = int(L)
        else:
            self.logger("TreeAnc: one_mutation and sequence length can't be reset",1)

    @property
    def one_mutation(self):
        """
        Returns
        -------
        float
            inverse of the uncompressed sequene length - length scale for short branches
        """
        L = self.seq_len
        if L:
            return 1.0/L

    @one_mutation.setter
    def one_mutation(self,om):
        self.logger("TreeAnc: one_mutation can't be set",1)


    @property
    def ref(self):
        """
        Get the str reference nucleotide sequence currently used by TreeAnc.
        When having read alignment in from a VCF, this is what variants map to.

        :setter: Sets the string reference sequence
        :getter: Returns the string reference sequence

        """
        return self._ref


    @ref.setter
    def ref(self, in_ref):
        """
        Parameters
        ----------
        in_ref : str
            reference sequence for the vcf sequence dict as a plain string
        """
        self._ref = in_ref


    def _attach_sequences_to_nodes(self):
        '''
        For each node of the tree, check whether there is a sequence available
        in the alignment and assign this sequence as a character array
        '''
        failed_leaves= 0
        if self.is_vcf:
            # if alignment is specified as difference from ref
            dic_aln = self.aln
        else:
            # if full alignment is specified
            dic_aln = {k.name: seq2array(k.seq, fill_overhangs=self.fill_overhangs,
                                                   ambiguous_character=self.gtr.ambiguous)
                                for k in self.aln} #

        # loop over leaves and assign multiplicities of leaves (e.g. number of identical reads)
        for l in self.tree.get_terminals():
            if l.name in self.seq_multiplicity:
                l.count = self.seq_multiplicity[l.name]
            else:
                l.count = 1.0


        # loop over tree, and assign sequences
        for l in self.tree.find_clades():
            if l.name in dic_aln:
                l.sequence= dic_aln[l.name]
            elif l.is_terminal():
                self.logger("***WARNING: TreeAnc._attach_sequences_to_nodes: NO SEQUENCE FOR LEAF: %s" % l.name, 0, warn=True)
                failed_leaves += 1
                l.sequence = seq2array(self.gtr.ambiguous*self.seq_len, fill_overhangs=self.fill_overhangs,
                                                 ambiguous_character=self.gtr.ambiguous)
                if failed_leaves > self.tree.count_terminals()/3:
                    self.logger("ERROR: At least 30\\% terminal nodes cannot be assigned with a sequence!\n", 0, warn=True)
                    self.logger("Are you sure the alignment belongs to the tree?", 2, warn=True)
                    break
            else: # could not assign sequence for internal node - is OK
                pass

        if failed_leaves:
            self.logger("***WARNING: TreeAnc: %d nodes don't have a matching sequence in the alignment."
                        " POSSIBLE ERROR."%failed_leaves, 0, warn=True)

        return self.make_reduced_alignment()


    def make_reduced_alignment(self):
        """
        Create the reduced alignment from the full sequences attached to (some)
        tree nodes. The methods collects all sequences from the tree nodes, creates
        the alignment, counts the multiplicity for each column of the alignment
        ('alignment pattern'), and creates the reduced alignment, where only the
        unique patterns are present. The reduced alignment and the pattern multiplicity
        are sufficient for the GTR calculations and allow to save memory on profile
        instantiation.

        The maps from full sequence to reduced sequence and back are also stored to allow
        compressing and expanding the sequences.

        Notes
        -----

          full_to_reduced_sequence_map : (array)
             Map to reduce a sequence

          reduced_to_full_sequence_map : (dict)
             Map to restore sequence from reduced alignment

          multiplicity : (array)
            Numpy array, which stores the pattern multiplicity for each position of the reduced alignment.

          reduced_alignment : (2D numpy array)
            The reduced alignment. Shape is (N x L'), where N is number of
            sequences, L' - number of unique alignment patterns

          cseq : (array)
            The compressed sequence (corresponding row of the reduced alignment) attached to each node

        """

        self.logger("TreeAnc: making reduced alignment...", 1)

        # bind positions in real sequence to that of the reduced (compressed) sequence
        self.full_to_reduced_sequence_map = np.zeros(self.seq_len, dtype=int)

        # bind position in reduced sequence to the array of positions in real (expanded) sequence
        self.reduced_to_full_sequence_map = {}

        #if is a dict, want to be efficient and not iterate over a bunch of const_sites
        #so pre-load alignment_patterns with the location of const sites!
        #and get the sites that we want to iterate over only!
        if self.is_vcf:
            tmp_reduced_aln, alignment_patterns, positions = self.process_alignment_dict()
            seqNames = self.aln.keys() #store seqName order to put back on tree
        else:
            # transpose real alignment, for ease of iteration
            alignment_patterns = {}
            tmp_reduced_aln = []
            # NOTE the order of tree traversal must be the same as below
            # for assigning the cseq attributes to the nodes.
            seqs = [n.sequence for n in self.tree.find_clades() if hasattr(n, 'sequence')]
            if len(np.unique([len(x) for x in seqs]))>1:
                self.logger("TreeAnc: Sequences differ in in length! ABORTING",0, warn=True)
                aln_transpose = None
                return
            else:
                aln_transpose = np.array(seqs).T
                positions = range(self.seq_len)

        for pi in positions:
            if self.is_vcf:
                pattern = [ self.aln[k][pi] if pi in self.aln[k].keys()
                            else self.ref[pi] for k,v in self.aln.items() ]
            else:
                pattern = aln_transpose[pi]

            str_pat = "".join(pattern)
            # if the column contains only one state and ambiguous nucleotides, replace
            # those with the state in other strains right away
            unique_letters = list(np.unique(pattern))
            if hasattr(self.gtr, "ambiguous"):
                if len(unique_letters)==2 and self.gtr.ambiguous in unique_letters:
                    other = [c for c in unique_letters if c!=self.gtr.ambiguous][0]
                    str_pat = str_pat.replace(self.gtr.ambiguous, other)
                    unique_letters = [other]
            # if there is a mutation in this column, give it its private pattern
            # this is required when sampling mutations from reconstructed profiles.
            # otherwise, all mutations corresponding to the same pattern will be coupled.
            if len(unique_letters)>1:
                str_pat += '_%d'%pi

            # if the pattern is not yet seen,
            if str_pat not in alignment_patterns:
                # bind the index in the reduced aln, index in sequence to the pattern string
                alignment_patterns[str_pat] = (len(tmp_reduced_aln), [pi])
                # append this pattern to the reduced alignment
                tmp_reduced_aln.append(pattern)
            else:
                # if the pattern is already seen, append the position in the real
                # sequence to the reduced aln<->sequence_pos_indexes map
                alignment_patterns[str_pat][1].append(pi)

        # count how many times each column is repeated in the real alignment
        self.multiplicity = np.zeros(len(alignment_patterns))
        for p, pos in alignment_patterns.values():
            self.multiplicity[p]=len(pos)

        # create the reduced alignment as np array
        self.reduced_alignment = np.array(tmp_reduced_aln).T

        # create map to compress a sequence
        for p, pos in alignment_patterns.values():
            self.full_to_reduced_sequence_map[np.array(pos)]=p

        # create a map to reconstruct full sequence from the reduced (compressed) sequence
        for p, val in alignment_patterns.items():
            self.reduced_to_full_sequence_map[val[0]]=np.array(val[1], dtype=int)

        # assign compressed sequences to all nodes of the tree, which have sequence assigned
        # for dict we cannot assume this is in the same order, as it does below!
        # so do it explicitly
        #
        # sequences are overwritten during reconstruction and
        # ambiguous sites change. Keep orgininals for reference
        if self.is_vcf:
            seq_reduce_align = {n:self.reduced_alignment[i]
                                for i, n in enumerate(seqNames)}
            for n in self.tree.find_clades():
                if hasattr(n, 'sequence'):
                    n.original_cseq = seq_reduce_align[n.name]
                    n.cseq = np.copy(n.original_cseq)
        else:
            # NOTE the order of tree traversal must be the same as above to catch the
            # index in the reduced alignment correctly
            seq_count = 0
            for n in self.tree.find_clades():
                if hasattr(n, 'sequence'):
                    n.original_cseq = self.reduced_alignment[seq_count]
                    n.cseq = np.copy(n.original_cseq)
                    seq_count+=1

        self.logger("TreeAnc: constructed reduced alignment...", 1)
        return ttconf.SUCCESS


    def process_alignment_dict(self):
        """
        prepare the dictionary specifying differences from a reference sequence
        to construct the reduced alignment with variable sites only. NOTE:
            - sites can be constant but different from the reference
            - sites can be constant plus a ambiguous sites

        assigns
        -------
        - self.nonref_positions: at least one sequence is different from ref

        Returns
        -------
        reduced_alignment_const
            reduced alignment accounting for non-variable postitions

        alignment_patterns_const
            dict pattern -> (pos in reduced alignment, list of pos in full alignment)

        ariable_positions
            list of variable positions needed to construct remaining

        """

        # number of sequences in alignment
        nseq = len(self.aln)

        inv_map = defaultdict(list)
        for k,v in self.aln.items():
            for pos, bs in v.items():
                inv_map[pos].append(bs)

        self.nonref_positions = np.sort(list(inv_map.keys()))
        self.inferred_const_sites = []

        ambiguous_char = self.gtr.ambiguous
        nonref_const = []
        nonref_alleles = []
        ambiguous_const = []
        variable_pos = []
        for pos, bs in inv_map.items(): #loop over positions and patterns
            bases = "".join(np.unique(bs))
            if len(bs) == nseq:
                if (len(bases)<=2 and ambiguous_char in bases) or len(bases)==1:
                    # all sequences different from reference, but only one state
                    # (other than ambiguous_char) in column
                    nonref_const.append(pos)
                    nonref_alleles.append(bases.replace(ambiguous_char, ''))
                    if ambiguous_char in bases: #keep track of sites 'made constant'
                        self.inferred_const_sites.append(pos)
                else:
                    # at least two non-reference alleles
                    variable_pos.append(pos)
            else:
                # not every sequence different from reference
                if bases==ambiguous_char:
                    ambiguous_const.append(pos)
                    self.inferred_const_sites.append(pos) #keep track of sites 'made constant'
                else:
                    # at least one non ambiguous non-reference allele not in
                    # every sequence
                    variable_pos.append(pos)

        refMod = np.array(list(self.ref))
        # place constant non reference positions by their respective allele
        refMod[nonref_const] = nonref_alleles
        # mask variable positions
        states = self.gtr.alphabet
        # maybe states = np.unique(refMod)
        refMod[variable_pos] = '.'

        # for each base in the gtr, make constant alignment pattern and
        # assign it to all const positions in the modified reference sequence
        reduced_alignment_const = []
        alignment_patterns_const = {}
        for base in states:
            p = base*nseq
            pos = list(np.where(refMod==base)[0])
            #if the alignment doesn't have a const site of this base, don't add! (ex: no '----' site!)
            if len(pos):
                alignment_patterns_const[p] = [len(reduced_alignment_const), pos]
                reduced_alignment_const.append(list(p))


        return reduced_alignment_const, alignment_patterns_const, variable_pos


    def prepare_tree(self):
        """
        Set link to parent and calculate distance to root for all tree nodes.
        Should be run once the tree is read and after every rerooting,
        topology change or branch length optimizations.
        """
        self.tree.root.branch_length = 0.001
        self.tree.root.mutation_length = self.tree.root.branch_length
        self.tree.root.mutations = []
        self.tree.ladderize()
        self._prepare_nodes()
        self._leaves_lookup = {node.name:node for node in self.tree.get_terminals()}


    def _prepare_nodes(self):
        """
        Set auxilliary parameters to every node of the tree.
        """
        self.tree.root.up = None
        self.tree.root.bad_branch=self.tree.root.bad_branch if hasattr(self.tree.root, 'bad_branch') else False
        internal_node_count = 0
        for clade in self.tree.get_nonterminals(order='preorder'): # parents first
            internal_node_count+=1
            if clade.name is None:
                clade.name = "NODE_" + format(self._internal_node_count, '07d')
                self._internal_node_count += 1
            for c in clade.clades:
                if c.is_terminal():
                    c.bad_branch = c.bad_branch if hasattr(c, 'bad_branch') else False
                c.up = clade

        for clade in self.tree.get_nonterminals(order='postorder'): # parents first
            clade.bad_branch = all([c.bad_branch for c in clade])

        self._calc_dist2root()
        self._internal_node_count = max(internal_node_count, self._internal_node_count)


    def _calc_dist2root(self):
        """
        For each node in the tree, set its root-to-node distance as dist2root
        attribute
        """
        self.tree.root.dist2root = 0.0
        for clade in self.tree.get_nonterminals(order='preorder'): # parents first
            for c in clade.clades:
                if not hasattr(c, 'mutation_length'):
                    c.mutation_length=c.branch_length
                c.dist2root = c.up.dist2root + c.mutation_length



####################################################################
## END SET-UP
####################################################################

    def infer_gtr(self, print_raw=False, marginal=False, normalized_rate=True,
                  fixed_pi=None, pc=5.0, **kwargs):
        """
        Calculates a GTR model given the multiple sequence alignment and the tree.
        It performs ancestral sequence inferrence (joint or marginal), followed by
        the branch lengths optimization. Then, the numbers of mutations are counted
        in the optimal tree and related to the time within the mutation happened.
        From these statistics, the relative state transition probabilities are inferred,
        and the transition matrix is computed.

        The result is used to construct the new GTR model of type 'custom'.
        The model is assigned to the TreeAnc and is used in subsequent analysis.

        Parameters
        -----------

         print_raw : bool
            If True, print the inferred GTR model

         marginal : bool
            If True, use marginal sequence reconstruction

         normalized_rate : bool
            If True, sets the mutation rate prefactor to 1.0.

         fixed_pi : np.array
            Provide the equilibrium character concentrations.
            If None is passed, the concentrations will be inferred from the alignment.

         pc: float
            Number of pseudo counts to use in gtr inference

        Returns
        -------

         gtr : GTR
            The inferred GTR model
        """

        # decide which type of the Maximum-likelihood reconstruction use
        # (marginal) or (joint)
        if marginal:
            _ml_anc = self._ml_anc_marginal
        else:
            _ml_anc = self._ml_anc_joint

        self.logger("TreeAnc.infer_gtr: inferring the GTR model from the tree...", 1)
        if (self.tree is None) or (self.aln is None):
            self.logger("TreeAnc.infer_gtr: ERROR, alignment or tree are missing", 0)
            return ttconf.ERROR

        _ml_anc(final=True, **kwargs) # call one of the reconstruction types
        alpha = list(self.gtr.alphabet)
        n=len(alpha)
        nij = np.zeros((n,n))
        Ti = np.zeros(n)

        self.logger("TreeAnc.infer_gtr: counting mutations...", 2)
        for node in self.tree.find_clades():
            if hasattr(node,'mutations'):
                for a,pos, d in node.mutations:
                    i,j = alpha.index(a), alpha.index(d)
                    nij[i,j]+=1
                    Ti[i] += 0.5*self._branch_length_to_gtr(node)
                    Ti[j] -= 0.5*self._branch_length_to_gtr(node)
                for ni,nuc in enumerate(node.cseq):
                    i = alpha.index(nuc)
                    Ti[i] += self._branch_length_to_gtr(node)*self.multiplicity[ni]
        self.logger("TreeAnc.infer_gtr: counting mutations...done", 3)
        if print_raw:
            print('alphabet:',alpha)
            print('n_ij:', nij, nij.sum())
            print('T_i:', Ti, Ti.sum())
        root_state = np.array([np.sum((self.tree.root.cseq==nuc)*self.multiplicity) for nuc in alpha])

        self._gtr = GTR.infer(nij, Ti, root_state, fixed_pi=fixed_pi, pc=pc,
                              alphabet=self.gtr.alphabet, logger=self.logger,
                              prof_map = self.gtr.profile_map)

        if normalized_rate:
            self.logger("TreeAnc.infer_gtr: setting overall rate to 1.0...", 2)
            self._gtr.mu=1.0
        return self._gtr


###################################################################
### ancestral reconstruction
###################################################################
    def infer_ancestral_sequences(self,*args, **kwargs):
        """Shortcut for :py:meth:`treetime.TreeAnc.reconstruct_anc`
        """
        return self.reconstruct_anc(*args,**kwargs)


    def reconstruct_anc(self, method='probabilistic', infer_gtr=False,
                        marginal=False, **kwargs):
        """Reconstruct ancestral sequences

        Parameters
        ----------
        method : str
           Method to use. Supported values are "fitch" and "ml"

        infer_gtr : bool
           Infer a GTR model before reconstructing the sequences

        marginal : bool
           Assign sequences that are most likely after averaging over all other nodes
           instead of the jointly most likely sequences.
        **kwargs
            additional keyword arguments that are passed down to :py:meth:`TreeAnc.infer_gtr` and :py:meth:`TreeAnc._ml_anc`

        Returns
        -------
        N_diff : int
           Number of nucleotides different from the previous
           reconstruction.  If there were no pre-set sequences, returns N*L

        """
        self.logger("TreeAnc.infer_ancestral_sequences with method: %s, %s"%(method, 'marginal' if marginal else 'joint'), 1)
        if (self.tree is None) or (self.aln is None):
            self.logger("TreeAnc.infer_ancestral_sequences: ERROR, alignment or tree are missing", 0)
            return ttconf.ERROR

        if method in ['ml', 'probabilistic']:
            if marginal:
                _ml_anc = self._ml_anc_marginal
            else:
                _ml_anc = self._ml_anc_joint
        else:
            _ml_anc = self._fitch_anc

        if infer_gtr:
            tmp = self.infer_gtr(marginal=marginal, **kwargs)
            if tmp==ttconf.ERROR:
                return tmp
            N_diff = _ml_anc(**kwargs)
        else:
            N_diff = _ml_anc(**kwargs)

        return N_diff


    def recover_var_ambigs(self):
        """
        Recalculates mutations using the original compressed sequence for terminal nodes
        which will recover ambiguous bases at variable sites. (See 'get_mutations')

        Once this has been run, infer_gtr and other functions which depend on self.gtr.alphabet
        will not work, as ambiguous bases are not part of that alphabet (only A, C, G, T, -).
        This is why it's left for the user to choose when to run
        """
        for node in self.tree.get_terminals():
            node.mutations = self.get_mutations(node, keep_var_ambigs=True)


    def get_mutations(self, node, keep_var_ambigs=False):
        """
        Get the mutations on a tree branch. Take compressed sequences from both sides
        of the branch (attached to the node), compute mutations between them, and
        expand these mutations to the positions in the real sequences.

        Parameters
        ----------
        node : PhyloTree.Clade
           Tree node, which is the child node attached to the branch.

        keep_var_ambigs : boolean
           If true, generates mutations based on the *original* compressed sequence, which
           may include ambiguities. Note sites that only have 1 unambiguous base and ambiguous
           bases ("AAAAANN") are stripped of ambiguous bases *before* compression, so ambiguous
           bases will **not** be preserved.

        Returns
        -------
        muts : list
          List of mutations. Each mutation is represented as tuple of
          :code:`(parent_state, position, child_state)`.
        """

        # if ambiguous site are to be restored and node is terminal,
        # assign original sequence, else reconstructed cseq
        node_seq = node.cseq
        if keep_var_ambigs and hasattr(node, "original_cseq") and node.is_terminal():
            node_seq = node.original_cseq

        muts = []
        for p, (anc, der) in enumerate(zip(node.up.cseq, node_seq)):
            # only if the states in compressed sequences differ:
            if anc!=der:
                # expand to the positions in real sequence
                muts.extend([(anc, pos, der) for pos in self.reduced_to_full_sequence_map[p]])

        #sort by position
        return sorted(muts, key=lambda x:x[1])


    def get_branch_mutation_matrix(self, node, full_sequence=False):
        """uses results from marginal ancesrtal inference to return a joint
        distribution of the sequence states at both ends of the branch.

        Parameters
        ----------
        node : Phylo.clade
            node of the tree
        full_sequence : bool, optional
            expand the sequence to the full sequence, if false (default)
            the there will be one mutation matrix for each column in the
            reduced alignment

        Returns
        -------
        numpy.array
            an Lxqxq stack of matrices (q=alphabet size, L (reduced)sequence length)
        """
        from itertools import product
        pp,pc = self.marginal_branch_profile(node)
        if pp is None or pc is None:
            return None

        expQt = self.gtr.expQt(self._branch_length_to_gtr(node))
        mut_matrix_stack = np.zeros((pp.shape[1], pp.shape[1],pp.shape[0]))
        for i,j in product(range(pp.shape[1]), repeat=2):
            mut_matrix_stack[i,j,:] = pp[:,i]*pc[:,j]*expQt[j,i]

        normalizer = mut_matrix_stack.sum(axis=1).sum(axis=0)
        mut_matrix_stack = mut_matrix_stack/normalizer
        mut_matrix_stack = np.swapaxes(np.swapaxes(mut_matrix_stack, 1,2), 0,1)
        if full_sequence:
            return mut_matrix_stack[self.full_to_reduced_sequence_map]
        else:
            return mut_matrix_stack


    def expanded_sequence(self, node):
        """
        Get node's compressed sequence and expand it to the real sequence

        Parameters
        ----------
        node : PhyloTree.Clade
           Tree node

        Returns
        -------
        seq : np.array
           Sequence as np.array of chars
        """
        seq = np.zeros_like(self.full_to_reduced_sequence_map, dtype='U1')
        for pos, state in enumerate(node.cseq):
            seq[self.reduced_to_full_sequence_map[pos]] = state

        return seq


    def dict_sequence(self, node, keep_var_ambigs=False):
        """
        For VCF-based TreeAnc objects, we do not want to store the entire
        sequence on every node, as they could be large. Instead, this returns the dict
        of variants & their positions for this sequence. This is used in place of
        :py:meth:`treetime.TreeAnc.expanded_sequence` for VCF-based objects throughout TreeAnc. However, users can still
        call :py:meth:`expanded_sequence` if they require the full sequence.

        Parameters
        ----------
         node  : PhyloTree.Clade
            Tree node

        Returns
        -------
         seq : dict
            dict where keys are the basepair position (numbering from 0) and value is the variant call

        """
        seq = {}

        node_seq = node.cseq
        if keep_var_ambigs and hasattr(node, "original_cseq") and node.is_terminal():
            node_seq = node.original_cseq

        for pos in self.nonref_positions:
            cseqLoc = self.full_to_reduced_sequence_map[pos]
            base = node_seq[cseqLoc]
            if self.ref[pos] != base:
                seq[pos] = base

        return seq

###################################################################
### FITCH
###################################################################
    def _fitch_anc(self, **kwargs):
        """
        Reconstruct ancestral states using Fitch's algorithm. The method requires
        sequences to be assigned to leaves. It implements the iteration from
        leaves to the root constructing the Fitch profiles for each character of
        the sequence, and then by propagating from the root to the leaves,
        reconstructs the sequences of the internal nodes.

        Keyword Args
        ------------

        Returns
        -------
        Ndiff : int
           Number of the characters that changed since the previous
           reconstruction. These changes are determined from the pre-set
           sequence attributes of the nodes. If there are no sequences available
           (i.e., no reconstruction has been made before), returns the total
           number of characters in the tree.

        """
        # set fitch profiiles to each terminal node

        for l in self.tree.get_terminals():
            l.state = [[k] for k in l.cseq]

        L = len(self.tree.get_terminals()[0].cseq)

        self.logger("TreeAnc._fitch_anc: Walking up the tree, creating the Fitch profiles",2)
        for node in self.tree.get_nonterminals(order='postorder'):
            node.state = [self._fitch_state(node, k) for k in range(L)]

        ambs = [i for i in range(L) if len(self.tree.root.state[i])>1]
        if len(ambs) > 0:
            for amb in ambs:
                self.logger("Ambiguous state of the root sequence "
                                    "in the position %d: %s, "
                                    "choosing %s" % (amb, str(self.tree.root.state[amb]),
                                                     self.tree.root.state[amb][0]), 4)
        self.tree.root.cseq = np.array([k[np.random.randint(len(k)) if len(k)>1 else 0]
                                           for k in self.tree.root.state])

        if self.is_vcf:
            self.tree.root.sequence = self.dict_sequence(self.tree.root)
        else:
            self.tree.root.sequence = self.expanded_sequence(self.tree.root)


        self.logger("TreeAnc._fitch_anc: Walking down the self.tree, generating sequences from the "
                         "Fitch profiles.", 2)
        N_diff = 0
        for node in self.tree.get_nonterminals(order='preorder'):
            if node.up != None: # not root
                sequence =  np.array([node.up.cseq[i]
                        if node.up.cseq[i] in node.state[i]
                        else node.state[i][0] for i in range(L)])
                if hasattr(node, 'sequence'):
                    N_diff += (sequence!=node.cseq).sum()
                else:
                    N_diff += L
                node.cseq = sequence
                if self.is_vcf:
                    node.sequence = self.dict_sequence(node)
                else:
                    node.sequence = self.expanded_sequence(node)
                node.mutations = self.get_mutations(node)

            node.profile = seq2prof(node.cseq, self.gtr.profile_map)
            del node.state # no need to store Fitch states
        self.logger("Done ancestral state reconstruction",3)
        for node in self.tree.get_terminals():
            node.profile = seq2prof(node.original_cseq, self.gtr.profile_map)
        return N_diff


    def _fitch_state(self, node, pos):
        """
        Determine the Fitch profile for a single character of the node's sequence.
        The profile is essentially the intersection between the children's
        profiles or, if the former is empty, the union of the profiles.

        Parameters
        ----------

         node : PhyloTree.Clade:
            Internal node which the profiles are to be determined

         pos : int
            Position in the node's sequence which the profiles should
            be determinedf for.

        Returns
        -------
         state : numpy.array
            Fitch profile for the character at position pos of the given node.
        """
        state = self._fitch_intersect([k.state[pos] for k in node.clades])
        if len(state) == 0:
            state = np.concatenate([k.state[pos] for k in node.clades])
        return state


    def _fitch_intersect(self, arrays):
        """
        Find the intersection of any number of 1D arrays.
        Return the sorted, unique values that are in all of the input arrays.
        Adapted from numpy.lib.arraysetops.intersect1d
        """
        def pairwise_intersect(arr1, arr2):
            s2 = set(arr2)
            b3 = [val for val in arr1 if val in s2]
            return b3

        arrays = list(arrays) # allow assignment
        N = len(arrays)
        while N > 1:
            arr1 = arrays.pop()
            arr2 = arrays.pop()
            arr = pairwise_intersect(arr1, arr2)
            arrays.append(arr)
            N = len(arrays)

        return arrays[0]



###################################################################
### Maximum Likelihood
###################################################################
    def sequence_LH(self, pos=None, full_sequence=False):
        """return the likelihood of the observed sequences given the tree

        Parameters
        ----------
        pos : int, optional
            position in the sequence, if none, the sum over all positions will be returned
        full_sequence : bool, optional
            does the position refer to the full or compressed sequence, by default compressed sequence is assumed.

        Returns
        -------
        float
            likelihood
        """
        if not hasattr(self.tree, "total_sequence_LH"):
            self.logger("TreeAnc.sequence_LH: you need to run marginal ancestral inference first!", 1)
            self.infer_ancestral_sequences(marginal=True)
        if pos is not None:
            if full_sequence:
                compressed_pos = self.full_to_reduced_sequence_map[pos]
            else:
                compressed_pos = pos
            return self.tree.sequence_LH[compressed_pos]
        else:
            return self.tree.total_sequence_LH


    def ancestral_likelihood(self):
        """
        Calculate the likelihood of the given realization of the sequences in
        the tree

        Returns
        -------

         log_lh : float
            The tree likelihood given the sequences
        """
        log_lh = np.zeros(self.multiplicity.shape[0])
        for node in self.tree.find_clades(order='postorder'):

            if node.up is None: #  root node
                # 0-1 profile
                profile = seq2prof(node.cseq, self.gtr.profile_map)
                # get the probabilities to observe each nucleotide
                profile *= self.gtr.Pi
                profile = profile.sum(axis=1)
                log_lh += np.log(profile) # product over all characters
                continue

            t = node.branch_length

            indices = np.array([(np.argmax(self.gtr.alphabet==a),
                        np.argmax(self.gtr.alphabet==b)) for a, b in zip(node.up.cseq, node.cseq)])

            logQt = np.log(self.gtr.expQt(t))
            lh = logQt[indices[:, 1], indices[:, 0]]
            log_lh += lh

        return log_lh

    def _branch_length_to_gtr(self, node):
        """
        Set branch lengths to either mutation lengths of given branch lengths.
        The assigend values are to be used in the following ML analysis.
        """
        if self.use_mutation_length:
            return max(ttconf.MIN_BRANCH_LENGTH*self.one_mutation, node.mutation_length)
        else:
            return max(ttconf.MIN_BRANCH_LENGTH*self.one_mutation, node.branch_length)


    def _ml_anc_marginal(self, store_compressed=True, final=True, sample_from_profile=False,
                         debug=False, **kwargs):
        """
        Perform marginal ML reconstruction of the ancestral states. In contrast to
        joint reconstructions, this needs to access the probabilities rather than only
        log probabilities and is hence handled by a separate function.

        Parameters
        ----------

         store_compressed : bool, default True
            attach a reduced representation of sequence changed to each branch

         final : bool, default True
            stop full length by expanding sites with identical alignment patterns

         sample_from_profile : bool or str
            assign sequences probabilistically according to the inferred probabilities
            of ancestral states instead of to their ML value. This parameter can also
            take the value 'root' in which case probabilistic sampling will happen
            at the root but at no other node.

        """

        tree = self.tree
        # number of nucleotides changed from prev reconstruction
        N_diff = 0

        L = self.multiplicity.shape[0]
        n_states = self.gtr.alphabet.shape[0]
        self.logger("TreeAnc._ml_anc_marginal: type of reconstruction: Marginal", 2)

        self.logger("Walking up the tree, computing likelihoods... ", 3)
        #  set the leaves profiles
        for leaf in tree.get_terminals():
            # in any case, set the profile
            leaf.marginal_subtree_LH = seq2prof(leaf.original_cseq, self.gtr.profile_map)
            leaf.marginal_subtree_LH_prefactor = np.zeros(L)

        # propagate leaves --> root, set the marginal-likelihood messages
        for node in tree.get_nonterminals(order='postorder'): #leaves -> root
            # regardless of what was before, set the profile to ones
            tmp_log_subtree_LH = np.zeros((L,n_states))
            node.marginal_subtree_LH_prefactor = np.zeros(L)
            for ch in node.clades:
                ch.marginal_log_Lx = self.gtr.propagate_profile(ch.marginal_subtree_LH,
                    self._branch_length_to_gtr(ch), return_log=True) # raw prob to transfer prob up
                tmp_log_subtree_LH += ch.marginal_log_Lx
                node.marginal_subtree_LH_prefactor += ch.marginal_subtree_LH_prefactor

            tmp_prefactor = np.max(tmp_log_subtree_LH,axis=1)
            node.marginal_subtree_LH = np.exp(tmp_log_subtree_LH.T-tmp_prefactor).T
            pre = node.marginal_subtree_LH.sum(axis=1) #sum over nucleotide states
            node.marginal_subtree_LH = (node.marginal_subtree_LH.T/pre).T # normalize so that the sum is 1
            node.marginal_subtree_LH_prefactor += np.log(pre) + tmp_prefactor # and store log-prefactor

        self.logger("Computing root node sequence and total tree likelihood...",3)
        # Msg to the root from the distant part (equ frequencies)
        tree.root.marginal_outgroup_LH = np.repeat([self.gtr.Pi], tree.root.marginal_subtree_LH.shape[0], axis=0)

        tree.root.marginal_profile = tree.root.marginal_outgroup_LH*tree.root.marginal_subtree_LH
        pre = tree.root.marginal_profile.sum(axis=1)
        tree.root.marginal_profile = (tree.root.marginal_profile.T/pre).T
        marginal_LH_prefactor = tree.root.marginal_subtree_LH_prefactor + np.log(pre)

        # choose sequence characters from this profile.
        # treat root node differently to avoid piling up mutations on the longer branch
        if sample_from_profile=='root':
            root_sample_from_profile = True
            other_sample_from_profile = False
        elif isinstance(sample_from_profile, bool):
            root_sample_from_profile = sample_from_profile
            other_sample_from_profile = sample_from_profile

        seq, prof_vals, idxs = prof2seq(tree.root.marginal_profile,
                                        self.gtr, sample_from_prof=root_sample_from_profile)

        self.tree.sequence_LH = marginal_LH_prefactor
        self.tree.total_sequence_LH = (self.tree.sequence_LH*self.multiplicity).sum()
        self.tree.root.cseq = seq

        if final:
            if self.is_vcf:
                self.tree.root.sequence = self.dict_sequence(self.tree.root)
            else:
                self.tree.root.sequence = self.expanded_sequence(self.tree.root)

        self.logger("Walking down the tree, computing maximum likelihood sequences...",3)
        # propagate root -->> leaves, reconstruct the internal node sequences
        # provided the upstream message + the message from the complementary subtree
        for node in tree.find_clades(order='preorder'):
            if node.up is None: # skip if node is root
                continue

            # integrate the information coming from parents with the information
            # of all children my multiplying it to the prev computed profile
            tmp_msg = np.log(node.up.marginal_profile) - node.marginal_log_Lx
            tmp_prefactor = np.max(tmp_msg, axis=1)
            tmp_msg = np.exp(tmp_msg.T - tmp_prefactor).T

            norm_vector = tmp_msg.sum(axis=1)
            node.marginal_outgroup_LH = (tmp_msg.T/norm_vector).T
            tmp_msg_from_parent = self.gtr.propagate_profile(node.marginal_outgroup_LH,
                                                 self._branch_length_to_gtr(node), return_log=False)
            node.marginal_profile = node.marginal_subtree_LH * tmp_msg_from_parent

            norm_vector = node.marginal_profile.sum(axis=1)
            node.marginal_profile=(node.marginal_profile.T/norm_vector).T

            # choose sequence based maximal marginal LH.
            seq, prof_vals, idxs = prof2seq(node.marginal_profile, self.gtr,
                                                      sample_from_prof=other_sample_from_profile)

            if hasattr(node, 'cseq') and node.cseq is not None:
                N_diff += (seq!=node.cseq).sum()
            else:
                N_diff += L

            #assign new sequence
            node.cseq = seq
            if final:
                if self.is_vcf:
                    node.sequence = self.dict_sequence(node)
                else:
                    node.sequence = self.expanded_sequence(node)
                node.mutations = self.get_mutations(node)


        # note that the root doesn't contribute to N_diff (intended, since root sequence is often ambiguous)
        self.logger("TreeAnc._ml_anc_marginal: ...done", 3)
        if store_compressed:
            self._store_compressed_sequence_pairs()

        # do clean-up:
        if not debug:
            for node in self.tree.find_clades():
                try:
                    # del node.marginal_profile
                    # del node.marginal_outgroup_LH
                    del node.marginal_Lx
                except:
                    pass

        return N_diff


    def _ml_anc_joint(self, store_compressed=True, final=True, sample_from_profile=False,
                            debug=False, **kwargs):

        """
        Perform joint ML reconstruction of the ancestral states. In contrast to
        marginal reconstructions, this only needs to compare and multiply LH and
        can hence operate in log space.

        Parameters
        ----------

         store_compressed : bool, default True
            attach a reduced representation of sequence changed to each branch

         final : bool, default True
            stop full length by expanding sites with identical alignment patterns

         sample_from_profile : str
            This parameter can take the value 'root' in which case probabilistic
            sampling will happen at the root. otherwise sequences at ALL nodes are
            set to the value that jointly optimized the likelihood.

        """
        N_diff = 0 # number of sites differ from perv reconstruction
        L = self.multiplicity.shape[0]
        n_states = self.gtr.alphabet.shape[0]

        self.logger("TreeAnc._ml_anc_joint: type of reconstruction: Joint", 2)

        self.logger("TreeAnc._ml_anc_joint: Walking up the tree, computing likelihoods... ", 3)
        # for the internal nodes, scan over all states j of this node, maximize the likelihood
        for node in self.tree.find_clades(order='postorder'):
            if node.up is None:
                node.joint_Cx=None # not needed for root
                continue

            # preallocate storage
            node.joint_Lx = np.zeros((L, n_states))             # likelihood array
            node.joint_Cx = np.zeros((L, n_states), dtype=int)  # max LH indices
            branch_len = self._branch_length_to_gtr(node)
            # transition matrix from parent states to the current node states.
            # denoted as Pij(i), where j - parent state, i - node state
            log_transitions = np.log(self.gtr.expQt(branch_len))

            if node.is_terminal():
                try:
                    msg_from_children = np.log(np.maximum(seq2prof(node.original_cseq, self.gtr.profile_map), ttconf.TINY_NUMBER))
                except:
                    raise ValueError("sequence assignment to node "+node.name+" failed")
                msg_from_children[np.isnan(msg_from_children) | np.isinf(msg_from_children)] = -ttconf.BIG_NUMBER
            else:
                # Product (sum-Log) over all child subtree likelihoods.
                # this is prod_ch L_x(i)
                msg_from_children = np.sum(np.stack([c.joint_Lx for c in node.clades], axis=0), axis=0)

            # for every possible state of the parent node,
            # get the best state of the current node
            # and compute the likelihood of this state
            for char_i, char in enumerate(self.gtr.alphabet):
                # Pij(i) * L_ch(i) for given parent state j
                msg_to_parent = (log_transitions.T[char_i, :] + msg_from_children)
                # For this parent state, choose the best state of the current node:
                node.joint_Cx[:, char_i] = msg_to_parent.argmax(axis=1)
                # compute the likelihood of the best state of the current node
                # given the state of the parent (char_i)
                node.joint_Lx[:, char_i] = msg_to_parent.max(axis=1)

        # root node profile = likelihood of the total tree
        msg_from_children = np.sum(np.stack([c.joint_Lx for c in self.tree.root], axis = 0), axis=0)
        # Pi(i) * Prod_ch Lch(i)
        self.tree.root.joint_Lx = msg_from_children + np.log(self.gtr.Pi)
        normalized_profile = (self.tree.root.joint_Lx.T - self.tree.root.joint_Lx.max(axis=1)).T

        # choose sequence characters from this profile.
        # treat root node differently to avoid piling up mutations on the longer branch
        if sample_from_profile=='root':
            root_sample_from_profile = True
        elif isinstance(sample_from_profile, bool):
            root_sample_from_profile = sample_from_profile

        seq, anc_lh_vals, idxs = prof2seq(np.exp(normalized_profile),
                                    self.gtr, sample_from_prof = root_sample_from_profile)

        # compute the likelihood of the most probable root sequence
        self.tree.sequence_LH = np.choose(idxs, self.tree.root.joint_Lx.T)
        self.tree.sequence_joint_LH = (self.tree.sequence_LH*self.multiplicity).sum()
        self.tree.root.cseq = seq
        self.tree.root.seq_idx = idxs
        if final:
            if self.is_vcf:
                self.tree.root.sequence = self.dict_sequence(self.tree.root)
            else:
                self.tree.root.sequence = self.expanded_sequence(self.tree.root)

        self.logger("TreeAnc._ml_anc_joint: Walking down the tree, computing maximum likelihood sequences...",3)
        # for each node, resolve the conditioning on the parent node
        for node in self.tree.find_clades(order='preorder'):

            # root node has no mutations, everything else has been alread y set
            if node.up is None:
                node.mutations = []
                continue

            # choose the value of the Cx(i), corresponding to the state of the
            # parent node i. This is the state of the current node
            node.seq_idx = np.choose(node.up.seq_idx, node.joint_Cx.T)
            # reconstruct seq, etc
            tmp_sequence = np.choose(node.seq_idx, self.gtr.alphabet)
            if hasattr(node, 'sequence') and node.cseq is not None:
                N_diff += (tmp_sequence!=node.cseq).sum()
            else:
                N_diff += L

            node.cseq = tmp_sequence
            if final:
                node.mutations = self.get_mutations(node)
                if self.is_vcf:
                    node.sequence = self.dict_sequence(node)
                else:
                    node.sequence = self.expanded_sequence(node)


        self.logger("TreeAnc._ml_anc_joint: ...done", 3)
        if store_compressed:
            self._store_compressed_sequence_pairs()

        # do clean-up
        if not debug:
            for node in self.tree.find_clades(order='preorder'):
                del node.joint_Lx
                del node.joint_Cx
                del node.seq_idx

        return N_diff


    def _store_compressed_sequence_to_node(self, node):
        """
        make a compressed representation of a pair of sequences only counting
        the number of times a particular pair of states (e.g. (A,T)) is observed
        the the aligned sequences of parent and child.

        Parameters
        -----------

         node : PhyloTree.Clade
            Tree node. **Note** because the method operates
            on the sequences on both sides of a branch, sequence reconstruction
            must be performed prior to calling this method.

        """
        seq_pairs, multiplicity = self.gtr.compress_sequence_pair(node.up.cseq,
                                              node.cseq,
                                              pattern_multiplicity = self.multiplicity,
                                              ignore_gaps = self.ignore_gaps)
        node.compressed_sequence = {'pair':seq_pairs, 'multiplicity':multiplicity}


    def _store_compressed_sequence_pairs(self):
        """
        Traverse the tree, and for each node store the compressed sequence pair.
        **Note** sequence reconstruction should be performed prior to calling
        this method.
        """
        self.logger("TreeAnc._store_compressed_sequence_pairs...",2)
        for node in self.tree.find_clades():
            if node.up is None:
                continue
            self._store_compressed_sequence_to_node(node)
        self.logger("TreeAnc._store_compressed_sequence_pairs...done",3)


###################################################################
### Branch length
###################################################################
    def optimize_branch_len(self, **kwargs):
        return self.optimize_branch_length(**kwargs)

    def optimize_branch_length(self, mode='joint', **kwargs):
        """
        Perform optimization for the branch lengths of the whole tree or any
        subtree.

        **Note** this method assumes that each node stores information
        about its sequence as numpy.array object (node.sequence attribute).
        Therefore, before calling this method, sequence reconstruction with
        either of the available models must be performed.

        Parameters
        ----------

         mode : str
            Optimize branch length assuming the joint ML sequence assignment
            of both ends of the branch (:code:`joint`), or trace over all possible sequence
            assignments on both ends of the branch (:code:`marginal`) (slower, experimental).

         **kwargs :
            Keyword arguments

        Keyword Args
        ------------

         verbose : int
            Output level

         store_old : bool
            If True, the old lengths will be saved in :code:`node._old_dist` attribute.
            Useful for testing, and special post-processing.


        """

        self.logger("TreeAnc.optimize_branch_length: running branch length optimization in mode %s..."%mode,1)
        if (self.tree is None) or (self.aln is None):
            self.logger("TreeAnc.optimize_branch_length: ERROR, alignment or tree are missing", 0)
            return ttconf.ERROR

        store_old_dist = False

        if 'store_old' in kwargs:
            store_old_dist = kwargs['store_old']

        if mode=='marginal':
            # a marginal ancestral reconstruction is required for
            # marginal branch length inference
            if not hasattr(self.tree.root, "marginal_profile"):
                self.infer_ancestral_sequences(marginal=True)

        max_bl = 0
        for node in self.tree.find_clades(order='postorder'):
            if node.up is None: continue # this is the root
            if store_old_dist:
                node._old_length = node.branch_length

            if mode=='marginal':
                new_len = self.optimal_marginal_branch_length(node)
            elif mode=='joint':
                new_len = self.optimal_branch_length(node)
            else:
                self.logger("treeanc.optimize_branch_length: unsupported optimization mode",4, warn=True)
                new_len = node.branch_length

            if new_len < 0:
                continue

            self.logger("Optimization results: old_len=%.4e, new_len=%.4e, naive=%.4e"
                   " Updating branch length..."%(node.branch_length, new_len, len(node.mutations)*self.one_mutation), 5)

            node.branch_length = new_len
            node.mutation_length=new_len
            max_bl = max(max_bl, new_len)

        # as branch lengths changed, the params must be fixed
        self.tree.root.up = None
        self.tree.root.dist2root = 0.0
        if max_bl>0.15:
            self.logger("TreeAnc.optimize_branch_length: THIS TREE HAS LONG BRANCHES."
                        " \n\t ****TreeTime IS NOT DESIGNED TO OPTIMIZE LONG BRANCHES."
                        " \n\t ****PLEASE OPTIMIZE BRANCHES WITH ANOTHER TOOL AND RERUN WITH"
                        " \n\t ****branch_length_mode='input'", 0, warn=True)
        self._prepare_nodes()
        return ttconf.SUCCESS


    def optimize_branch_length_global(self, **kwargs):
        """
        EXPERIMENTAL GLOBAL OPTIMIZATION
        """

        self.logger("TreeAnc.optimize_branch_length_global: running branch length optimization...",1)

        def neg_log(s):
            for si, n in zip(s, self.tree.find_clades(order='preorder')):
                n.branch_length = si**2

            self.infer_ancestral_sequences(marginal=True)

            gradient = []
            for si, n in zip(s, self.tree.find_clades(order='preorder')):
                if n.up:
                    pp, pc = self.marginal_branch_profile(n)
                    Qtds = self.gtr.expQsds(si).T
                    Qt = self.gtr.expQs(si).T

                    res = pp.dot(Qt)
                    overlap = np.sum(res*pc, axis=1)

                    res_ds = pp.dot(Qtds)
                    overlap_ds = np.sum(res_ds*pc, axis=1)
                    logP = np.sum(self.multiplicity*overlap_ds/overlap)

                    gradient.append(logP)
                else:
                    gradient.append(2*(si**2-0.001))

            print(-self.tree.sequence_marginal_LH)
            return (-self.tree.sequence_marginal_LH + (s[0]**2-0.001)**2, -1.0*np.array(gradient))

        from scipy.optimize import minimize
        x0 = np.sqrt([n.branch_length for n in self.tree.find_clades(order='preorder')])
        sol = minimize(neg_log, x0, jac=True)

        for new_len, node in zip(sol['x'], self.tree.find_clades()):
            self.logger("Optimization results: old_len=%.4f, new_len=%.4f "
                   " Updating branch length..."%(node.branch_length, new_len), 5)

            node.branch_length = new_len**2
            node.mutation_length=new_len**2

        # as branch lengths changed, the params must be fixed
        self.tree.root.up = None
        self.tree.root.dist2root = 0.0
        self._prepare_nodes()


    def optimal_branch_length(self, node):
        '''
        Calculate optimal branch length given the sequences of node and parent

        Parameters
        ----------
        node : PhyloTree.Clade
           TreeNode, attached to the branch.

        Returns
        -------
        new_len : float
           Optimal length of the given branch

        '''
        if node.up is None:
            return self.one_mutation

        parent = node.up
        if hasattr(node, 'compressed_sequence'):
            new_len = self.gtr.optimal_t_compressed(node.compressed_sequence['pair'],
                                                    node.compressed_sequence['multiplicity'])
        else:
            new_len = self.gtr.optimal_t(parent.cseq, node.cseq,
                                         pattern_multiplicity=self.multiplicity,
                                         ignore_gaps=self.ignore_gaps)
        return new_len


    def marginal_branch_profile(self, node):
        '''
        calculate the marginal distribution of sequence states on both ends
        of the branch leading to node,

        Parameters
        ----------
        node : PhyloTree.Clade
           TreeNode, attached to the branch.


        Returns
        -------
        pp, pc : Pair of vectors (profile parent, pp) and (profile child, pc)
           that are of shape (L,n) where L is sequence length and n is alphabet size.
           note that this correspond to the compressed sequences.
        '''
        parent = node.up
        if parent is None:
            self.logger("Branch profiles can't be calculated for the root!",3)
            return None, None
        if not hasattr(node, 'marginal_outgroup_LH'):
            self.logger("marginal ancestral inference needs to be performed first", 3)
            return None, None

        pc = node.marginal_subtree_LH
        pp = node.marginal_outgroup_LH
        return pp, pc


    def optimal_marginal_branch_length(self, node):
        '''
        calculate the marginal distribution of sequence states on both ends
        of the branch leading to node,

        Parameters
        ----------
        node : PhyloTree.Clade
           TreeNode, attached to the branch.

        Returns
        -------
        branch_length : float
           branch length of the branch leading to the node.
           note: this can be unstable on iteration
        '''

        if node.up is None:
            return self.one_mutation
        pp, pc = self.marginal_branch_profile(node)
        return self.gtr.optimal_t_compressed((pp, pc), self.multiplicity, profiles=True)


    def prune_short_branches(self):
        """
        If the branch length is less than the minimal value, remove the branch
        from the tree. **Requires** ancestral sequence reconstruction
        """
        self.logger("TreeAnc.prune_short_branches: pruning short branches (max prob at zero)...", 1)
        for node in self.tree.find_clades():
            if node.up is None or node.is_terminal():
                continue

            # probability of the two seqs separated by zero time is not zero
            if self.gtr.prob_t(node.up.cseq, node.cseq, 0.0,
                               pattern_multiplicity=self.multiplicity) > 0.1:
                # re-assign the node children directly to its parent
                node.up.clades = [k for k in node.up.clades if k != node] + node.clades
                for clade in node.clades:
                    clade.up = node.up


    def optimize_sequences_and_branch_length(self,*args, **kwargs):
        """This method is a shortcut for :py:meth:`treetime.TreeAnc.optimize_seq_and_branch_len`

        """
        self.optimize_seq_and_branch_len(*args,**kwargs)


    def optimize_seq_and_branch_len(self,reuse_branch_len=True, prune_short=True,
                                    marginal_sequences=False, branch_length_mode='joint',
                                    max_iter=5, infer_gtr=False, **kwargs):
        """
        Iteratively set branch lengths and reconstruct ancestral sequences until
        the values of either former or latter do not change. The algorithm assumes
        knowing only the topology of the tree, and requires that sequences are assigned
        to all leaves of the tree.

        The first step is to pre-reconstruct ancestral
        states using Fitch reconstruction algorithm or ML using existing branch length
        estimates. Then, optimize branch lengths and re-do reconstruction until
        convergence using ML method.

        Parameters
        -----------

         reuse_branch_len : bool
            If True, rely on the initial branch lengths, and start with the
            maximum-likelihood ancestral sequence inference using existing branch
            lengths. Otherwise, do initial reconstruction of ancestral states with
            Fitch algorithm, which uses only the tree topology.

         prune_short : bool
            If True, the branches with zero optimal length will be pruned from
            the tree, creating polytomies. The polytomies could be further
            processed using :py:meth:`treetime.TreeTime.resolve_polytomies` from the TreeTime class.

         marginal_sequences : bool
            Assign sequences to their marginally most likely value, rather than
            the values that are jointly most likely across all nodes.

         branch_length_mode : str
            'joint', 'marginal', or 'input'. Branch lengths are left unchanged in case
            of 'input'. 'joint' and 'marginal' cause branch length optimization
            while setting sequences to the ML value or tracing over all possible
            internal sequence states.

         max_iter : int
            Maximal number of times sequence and branch length iteration are optimized

         infer_gtr : bool
            Infer a GTR model from the observed substitutions.

        """
        if branch_length_mode=='marginal':
            marginal_sequences = True

        self.logger("TreeAnc.optimize_sequences_and_branch_length: sequences...", 1)
        if reuse_branch_len:
            N_diff = self.reconstruct_anc(method='probabilistic', infer_gtr=infer_gtr,
                                          marginal=marginal_sequences, **kwargs)
            self.optimize_branch_len(verbose=0, store_old=False, mode=branch_length_mode)
        else:
            N_diff = self.reconstruct_anc(method='fitch', infer_gtr=infer_gtr, **kwargs)

            self.optimize_branch_len(verbose=0, store_old=False, marginal=False)

        n = 0
        while n<max_iter:
            n += 1
            if prune_short:
                self.prune_short_branches()
            N_diff = self.reconstruct_anc(method='probabilistic', infer_gtr=False,
                                          marginal=marginal_sequences, **kwargs)

            self.logger("TreeAnc.optimize_sequences_and_branch_length: Iteration %d."
                   " #Nuc changed since prev reconstructions: %d" %(n, N_diff), 2)

            if N_diff < 1:
                break
            self.optimize_branch_len(verbose=0, store_old=False, mode=branch_length_mode)

        self.tree.unconstrained_sequence_LH = (self.tree.sequence_LH*self.multiplicity).sum()
        self._prepare_nodes() # fix dist2root and up-links after reconstruction
        self.logger("TreeAnc.optimize_sequences_and_branch_length: Unconstrained sequence LH:%f" % self.tree.unconstrained_sequence_LH , 2)
        return ttconf.SUCCESS


###############################################################################
### Utility functions
###############################################################################
    def get_reconstructed_alignment(self):
        """
        Get the multiple sequence alignment, including reconstructed sequences for
        the internal nodes.

        Returns
        -------
        new_aln : MultipleSeqAlignment
           Alignment including sequences of all internal nodes

        """
        from Bio.Align import MultipleSeqAlignment
        from Bio.Seq import Seq
        from Bio.SeqRecord import SeqRecord
        self.logger("TreeAnc.get_reconstructed_alignment ...",2)
        if not hasattr(self.tree.root, 'sequence'):
            self.logger("TreeAnc.reconstructed_alignment... reconstruction not yet done",3)
            self.reconstruct_anc('probabilistic')

        new_aln = MultipleSeqAlignment([SeqRecord(id=n.name, seq=Seq("".join(n.sequence)), description="")
                                        for n in self.tree.find_clades()])

        return new_aln


    def get_tree_dict(self, keep_var_ambigs=False):
        """
        For VCF-based objects, returns a nested dict with all the information required to
        reconstruct sequences for all nodes (terminal and internal).

        Parameters
        ----------
        keep_var_ambigs : boolean
            If true, generates dict sequences based on the *original* compressed sequences, which
            may include ambiguities. Note sites that only have 1 unambiguous base and ambiguous
            bases ("AAAAANN") are stripped of ambiguous bases *before* compression, so ambiguous
            bases at this sites will *not* be preserved.


        Returns
        -------
        tree_dict : dict
           Format: ::

               {
               'reference':'AGCTCGA...A',
               'sequences': { 'seq1':{4:'A', 7:'-'}, 'seq2':{100:'C'} },
               'positions': [1,4,7,10,100...],
               'inferred_const_sites': [7,100....]
               }

           reference: str
               The reference sequence to which the variable sites are mapped
           sequences: nested dict
               A dict for each sequence with the position and alternative call for each variant
           positions: list
               All variable positions in the alignment
           inferred_cost_sites: list
               *(optional)* Positions that were constant except ambiguous bases, which were
               converted into constant sites by TreeAnc (ex: 'AAAN' -> 'AAAA')

        Raises
        ------
        TypeError
            Description

        """
        if self.is_vcf:
            tree_dict = {}
            tree_dict['reference'] = self.ref
            tree_dict['positions'] = self.nonref_positions

            tree_aln = {}
            for n in self.tree.find_clades():
                if hasattr(n, 'sequence'):
                    if keep_var_ambigs: #regenerate dict to include ambig bases
                        tree_aln[n.name] = self.dict_sequence(n, keep_var_ambigs)
                    else:
                        tree_aln[n.name] = n.sequence

            tree_dict['sequences'] = tree_aln

            if len(self.inferred_const_sites) != 0:
                tree_dict['inferred_const_sites'] = self.inferred_const_sites

            return tree_dict
        else:
            raise TypeError("A dict can only be returned for trees created with VCF-input!")

