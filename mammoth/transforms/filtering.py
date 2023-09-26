from onmt.transforms import register_transform
from .transform import Transform, ObservableStats
import regex as re

class FilterTooLongStats(ObservableStats):
    """Runing statistics for FilterTooLongTransform."""

    __slots__ = ["filtered"]

    def __init__(self):
        self.filtered = 1

    def update(self, other: "FilterTooLongStats"):
        self.filtered += other.filtered

@register_transform(name='filtertoolong')
class FilterTooLongTransform(Transform):
    """Filter out sentence that are too long."""

    def __init__(self, opts):
        super().__init__(opts)

    @classmethod
    def add_options(cls, parser):
        """Avalilable options relate to this Transform."""
        group = parser.add_argument_group("Transform/Filter")
        group.add("--src_seq_length", "-src_seq_length", type=int, default=200, help="Maximum source sequence length.")
        group.add("--tgt_seq_length", "-tgt_seq_length", type=int, default=200, help="Maximum target sequence length.")

    def _parse_opts(self):
        self.src_seq_length = self.opts.src_seq_length
        self.tgt_seq_length = self.opts.tgt_seq_length

    def apply(self, example, is_train=False, stats=None, **kwargs):
        """Return None if too long else return as is."""
        src_len = len(example['src'])
        tgt_len = len(example['tgt'])
        if src_len == 0 or tgt_len == 0:
            # also filter empty strings
            return None
        if src_len > self.src_seq_length or tgt_len > self.tgt_seq_length:
            if stats is not None:
                stats.update(FilterTooLongStats())
            return None
        else:
            return example

    def _repr_args(self):
        """Return str represent key arguments for class."""
        return '{}={}, {}={}'.format('src_seq_length', self.src_seq_length, 'tgt_seq_length', self.tgt_seq_length)

# Filters inspired by OpusFilter https://github.com/Helsinki-NLP/OpusFilter/blob/aca40bd064d9b087c5216de0568d7fb91a31d142/opusfilter/filters.py

@register_transform(name='filterwordratio')
class FilterWordRatio(Transform):
    """Filter out sentence based on word length ratio"""

    def __init__(self, opts):
        super().__init__(opts)

    @classmethod
    def add_options(cls, parser):
        """Avalilable options relate to this Transform."""
        group = parser.add_argument_group("Transform/Filter")
        group.add("--word_ratio_threshold", "-word_ratio_threshold", type=int, default=3, help="Threshold for discarding sentences based on word ratio.")

    def _parse_opts(self):
        self.word_ratio_threshold = self.opts.word_ratio_threshold

    def apply(self, example, **kwargs):
        """Return None if too long else return as is."""
        src_len = len(example['src'])
        tgt_len = len(example['tgt'])
        lengths = sorted([src_len, tgt_len])
        if lengths[0] == 0:
            return None
        else:
            ratio = lengths[-1] / lengths[0]
            if ratio < self.word_ratio_threshold:
                return example
            else:
                return None

    def _repr_args(self):
        """Return str represent key arguments for class."""
        return '{}={}'.format('word_ratio_threshold', self.word_ratio_threshold)

@register_transform(name='filterrepetitions')
class FilterRepetitions(Transform):
    """Filter segments with repeated content. Useful e.g. for filtering data generated by a low-quality NMT model."""

    def __init__(self, opts):
        super().__init__(opts)

    @classmethod
    def add_options(cls, parser):
        """Avalilable options relate to this Transform."""
        group = parser.add_argument_group("Transform/Filter")
        group.add("--rep_threshold", "-rep_threshold", type=int, default=2, help="Number of times the substring is repeated.")
        group.add("--rep_min_len", "-rep_min_len", type=int, default=3, help="Minimum length of the repeated pattern.")
        group.add("--rep_max_len", "-rep_max_len", type=int, default=100, help="Maximum length of the repeated pattern.")

    def _parse_opts(self):
        self.rep_threshold = self.opts.rep_threshold
        self.rep_min_len = self.opts.rep_min_len
        self.rep_max_len = self.opts.rep_max_len

    def apply(self, example, **kwargs):
        """Return None if the repeated pattern appears more than n-threshold times."""
        # compiled regexp for finding repetitions
        rstring = f'(\\S.{{{self.rep_min_len-1},{self.rep_max_len}}}?)(?: *\\1){{{self.rep_threshold},}}'
        regex = re.compile(rstring)
        reps = []
        for segment in example['src'], example['tgt']:
            match = regex.search(' '.join(segment))
            print(match)
            if match:
                full = match.group(0)
                repeated = match.group(1)
                rep = full.count(repeated) - 1
            else:
                rep = 0
            reps.append(rep)
        print(reps)
        if max(reps) > self.rep_threshold:
            return None
        else:
            return example

    def _repr_args(self):
        """Return str represent key arguments for class."""
        return '{}={}, {}={}, {}={}'.format('rep_threshold', self.rep_threshold, 'rep_min_len', self.rep_min_len, 'rep_max_len', self.rep_max_len)
