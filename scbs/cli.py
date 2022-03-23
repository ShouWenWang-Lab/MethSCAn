import numba
import click
from datetime import datetime, timedelta
from collections import OrderedDict
from click import style
from click_help_colors import HelpColorsGroup
from .scbs import scan, echo
from .utils import _get_filepath
from .prepare import prepare
from .profile import profile
from .smooth import smooth
from .matrix import matrix
from .filter import filter_
from . import __version__


class Timer(object):
    def __init__(self, label="run", fmt="%a %b %d %H:%M:%S %Y"):
        self.label = style(label, bold=True)
        self.fmt = fmt
        self.begin_time = datetime.now()
        echo(f"\nStarted {self.label} on {self.begin_time.strftime(self.fmt)}.")
        return

    def stop(self):
        end_time = datetime.now()
        runtime = timedelta(seconds=(end_time - self.begin_time).seconds)
        echo(
            f"\nFinished {self.label} on {end_time.strftime(self.fmt)}. "
            f"Total runtime was {runtime} (hour:min:s)."
        )
        return


class OrderedGroup(HelpColorsGroup):
    def __init__(self, commands=None, *args, **kwargs):
        super(OrderedGroup, self).__init__(*args, **kwargs)
        self.commands = commands or OrderedDict()

    def list_commands(self, ctx):
        return self.commands


def _print_kwargs(kwargs):
    echo("\nCommand line arguments:")
    for arg, value in kwargs.items():
        if value is not None:
            value_fmt = style(str(_get_filepath(value)), fg="blue")
            echo(f"{arg: >15}: {value_fmt}")
    echo()


def _set_n_threads(ctx, param, value):
    """
    Set the number of CPU threads for numba.
    Arguments come straight from click option.
    """
    if value == -1:
        return numba.config.NUMBA_NUM_THREADS
    elif value == 0:
        return 1
    else:
        return value


# command group
@click.group(
    cls=OrderedGroup,
    help_headers_color="bright_white",
    help_options_color="green",
    help=f"""
        \b
                    {style("|", fg="blue")}
        {style(",---. ,---. |---. ,---.", fg="blue")} \
{style("version " + __version__, fg="green")}
        {style("`---. |     |   | `---.", fg="blue")}
        {style("`---' `---' `---' `---'", fg="blue")}

        Below you find a list of all available commands.
        To find out what they do and how to use them, check
        their help like this:

        {style("scbs [command] --help", fg="blue")}

        To use stdin or stdout, use the dash character
        {style("-", fg="blue")} instead of a file path.
        """,
)
@click.version_option()
def cli():
    pass


# prepare command
@cli.command(
    name="prepare",
    help=f"""
    Gathers single cell methylation data from multiple input files
    (one per cell) and creates a sparse matrix (position x cell) in CSR
    format for each chromosome. Methylated sites are represented by a 1,
    unmethylated sites are -1, missing values and other bases are 0.

    {style("INPUT_FILES", fg="green")} are single cell
    methylation files, for example '.cov'-files generated by Bismark.

    {style("DATA_DIR", fg="green")} is the output directory
    where the methylation data will be stored.

    Note: If you have many cells and encounter a "too many open files"-
    error, you need to increase the open file limit with e.g.
    'ulimit -n 9999'.
    """,
    short_help="Collect and store sc-methylation data for quick access",
    no_args_is_help=True,
)
@click.argument("input-files", type=click.File("rb"), nargs=-1)
@click.argument(
    "data-dir",
    type=click.Path(dir_okay=True, file_okay=False, writable=True),
)
@click.option(
    "--input-format",
    default="bismark",
    help="""
    Specify the format of the input files. Options: 'bismark' (default),
    'methylpy', 'allc' or custom (see below).

    \b
    You can specify a custom format by specifying the separator, whether the
    file has a header, and which information is stored in which columns. These
    values should be separated by ':' and enclosed by quotation marks, for
    example --input-format '1:2:3:4m:\\t:1'

    \b
    The six ':'-separated values denote:
    1. The column number that contains the chromosome name
    2. The column number that contains the genomic position
    3. The column number that contains the methylated counts
    4. The column number that contains either unmethylated counts (u) or the total
    coverage (c) followed by either 'm' or 'c', e.g. '4c' to denote that the 4th column
    contains the coverage
    5. The separator, e.g. '\\t' for tsv files or ',' for csv
    6. Either '1' if the file has a header or '0' if it does not have a header
    All column numbers are 1-indexed, i.e. to define the first column use '1' and not
    '0'.""",
)
def prepare_cli(**kwargs):
    timer = Timer(label="prepare")
    _print_kwargs(kwargs)
    prepare(**kwargs)
    timer.stop()


# smooth command
@cli.command(
    name="smooth",
    help=f"""
    This script will calculate the smoothed mean methylation over the
    whole genome.

    {style("DATA_DIR", fg="green")} is the directory containing the methylation matrices
    produced by running 'scbs prepare'.

    The smoothed methylation values will be written to
    {style("DATA_DIR/smoothed/", fg="green")}.
    """,
    short_help="Smooth the pseudobulk of single cell methylation data",
    no_args_is_help=True,
)
@click.argument(
    "data-dir",
    type=click.Path(
        exists=True, dir_okay=True, file_okay=False, readable=True, writable=True
    ),
)
@click.option(
    "-bw",
    "--bandwidth",
    default=1000,
    type=click.IntRange(min=1, max=1e6),
    metavar="INTEGER",
    show_default=True,
    help="Smoothing bandwidth in basepairs.",
)
@click.option(
    "--use-weights",
    is_flag=True,
    help="Use this to weigh each methylation site by log1p(coverage).",
)
def smooth_cli(**kwargs):
    timer = Timer(label="smooth")
    _print_kwargs(kwargs)
    smooth(**kwargs)
    timer.stop()


# scan command
@cli.command(
    name="scan",
    help=f"""
    Scans the whole genome for regions of variable methylation. This works by sliding
    a window across the genome, calculating the variance of methylation per window,
    and selecting windows above a variance threshold.

    {style("DATA_DIR", fg="green")} is the directory containing the methylation
    matrices produced by running 'scbs prepare', as well as the smoothed methylation
    values produced by running 'scbs smooth'.

    {style("OUTPUT", fg="green")} is the path of the output file in '.bed' format,
    containing the variable windows that were found.
    """,
    short_help="Scan the genome to discover regions with variable methylation",
    no_args_is_help=True,
)
@click.argument(
    "data-dir",
    type=click.Path(exists=True, dir_okay=True, file_okay=False, readable=True),
)
@click.argument("output", type=click.File("w"))
@click.option(
    "-bw",
    "--bandwidth",
    default=2000,
    type=click.IntRange(min=1, max=1e6),
    metavar="INTEGER",
    show_default=True,
    help="Bandwidth of the variance windows in basepairs.",
)
@click.option(
    "--stepsize",
    default=10,
    type=click.IntRange(min=1, max=1e6),
    metavar="INTEGER",
    show_default=True,
    help="Step size of the variance windows in basepairs.",
)
@click.option(
    "--var-threshold",
    default=0.02,
    show_default=True,
    type=click.FloatRange(min=0, max=1),
    metavar="FLOAT",
    help="The variance threshold, i.e. 0.02 means that the top 2% "
    "most variable genomic bins will be reported. Overlapping variable bins "
    "are merged.",
)
@click.option(
    "--threads",
    default=-1,
    help="How many CPU threads to use in parallel.  [default: all available]",
    callback=_set_n_threads,
)
def scan_cli(**kwargs):
    timer = Timer(label="scan")
    _print_kwargs(kwargs)
    scan(**kwargs)
    timer.stop()


# matrix command (makes a "count" matrix)
@cli.command(
    name="matrix",
    help=f"""
    From single cell methylation or NOMe-seq data, calculates the average methylation
    in genomic regions for every cell. The output is a long table that can be used e.g.
    for dimensionality reduction or clustering, analogous to a count matrix in
    scRNA-seq.

    {style("REGIONS", fg="green")} is an alphabetically sorted (!) .bed file of regions
    for which methylation will be quantified in every cell.

    {style("DATA_DIR", fg="green")} is the directory containing the methylation
    matrices produced by running 'scbs prepare', as well as the smoothed methylation
    values produced by running 'scbs smooth'.

    {style("OUTPUT", fg="green")} is the file path where the count table will be
    written. Should end with '.csv'. The table is in long format and missing values
    are omitted.
    """,
    short_help="Make a methylation matrix, similar to a count matrix in scRNA-seq",
    no_args_is_help=True,
)
@click.argument("regions", type=click.File("r"))
@click.argument(
    "data-dir",
    type=click.Path(exists=True, dir_okay=True, file_okay=False, readable=True),
)
@click.argument("output", type=click.File("w"))
@click.option(
    "--keep-other-columns",
    is_flag=True,
    help="Use this to keep any other columns that the input bed-file may contain.",
)
def matrix_cli(**kwargs):
    timer = Timer(label="matrix")
    _print_kwargs(kwargs)
    matrix(**kwargs)
    timer.stop()


# profile command
@cli.command(
    name="profile",
    help=f"""
    From single cell methylation or NOMe-seq data,
    calculates the average methylation profile of a set of
    genomic regions. Useful for plotting and visually comparing
    methylation between groups of regions or cells.

    {style("REGIONS", fg="green")} is an alphabetically sorted (!) .bed file of regions
    for which the methylation profile will be produced.

    {style("DATA_DIR", fg="green")} is the directory containing the methylation matrices
    produced by running 'scbs prepare'.

    {style("OUTPUT", fg="green")} is the file path where the methylation profile data
    will be written. Should end with '.csv'.
    """,
    short_help="Plot mean methylation around a group of genomic features",
    no_args_is_help=True,
)
@click.argument("regions", type=click.File("r"))
@click.argument(
    "data-dir",
    type=click.Path(exists=True, dir_okay=True, file_okay=False, readable=True),
)
@click.argument("output", type=click.File("w"))
@click.option(
    "--width",
    default=4000,
    show_default=True,
    type=click.IntRange(min=1, max=None),
    metavar="INTEGER",
    help="The total width of the profile plot in bp. "
    "The center of all bed regions will be "
    "extended in both directions by half of this amount. "
    "Shorter regions will be extended, longer regions "
    "will be shortened accordingly.",
)
@click.option(
    "--strand-column",
    type=click.IntRange(min=1, max=None),
    metavar="INTEGER",
    help="The bed column number (1-indexed) denoting "
    "the DNA strand of the region  [optional].",
)
@click.option(
    "--label",
    help="Specify a constant value to be added as a "
    "column to the output table. This can be "
    "useful to give each output a unique label when "
    "you want to concatenate multiple outputs  [optional].",
)
def profile_cli(**kwargs):
    timer = Timer(label="profile")
    _print_kwargs(kwargs)
    profile(**kwargs)
    timer.stop()


# filter command
@cli.command(
    name="filter",
    help=f"""
    Filters low-quality cells based on the number of observed methylation sites
    and/or the global methylation percentage.

    Alternatively, you may also provide a text file with the names of the cells you
    want to keep.

    {style("DATA_DIR", fg="green")} is the unfiltered directory containing the
    methylation matrices produced by running 'scbs prepare'.

    {style("FILTERED_DIR", fg="green")} is the output directory storing methylation
    data only for the cells that passed all filtering criteria.
    """,
    short_help="Filter low-quality cells based on coverage and mean methylation",
    no_args_is_help=True,
)
@click.argument(
    "data-dir",
    type=click.Path(exists=True, dir_okay=True, file_okay=False, readable=True),
)
@click.argument(
    "filtered-dir",
    type=click.Path(dir_okay=True, file_okay=False, writable=True),
)
@click.option(
    "--min-sites",
    type=click.IntRange(min=1),
    metavar="INTEGER",
    help="Minimum number of methylation sites required for a cell to pass filtering.",
)
@click.option(
    "--max-sites",
    type=click.IntRange(min=1),
    metavar="INTEGER",
    help="Maximum number of methylation sites required for a cell to pass filtering.",
)
@click.option(
    "--min-meth",
    type=click.FloatRange(min=0, max=100),
    metavar="PERCENT",
    help="Minimum average methylation percentage required for a cell to "
    "pass filtering.",
)
@click.option(
    "--max-meth",
    type=click.FloatRange(min=0, max=100),
    metavar="PERCENT",
    help="Maximum average methylation percentage required for a cell to "
    "pass filtering.",
)
@click.option(
    "--cell-names",
    type=click.File("r"),
    help="A text file with the names of the cells you want to keep (default) "
    "or remove. "
    "This is an alternative to the min/max filtering options. Each cell name "
    "must be on a new line.",
)
@click.option("--keep/--discard", default=True,
    help="Specify whether the cells listed in your text file should be kept (default) "
    "or discarded from the data set. Only use together with --cell-names."
)
def filter_cli(**kwargs):
    timer = Timer(label="filter")
    _print_kwargs(kwargs)
    filter_(**kwargs)
    timer.stop()


# CLI template:
# @cli.command(
#     help=f"""
#     Blabla

#     {style("INPUT", fg="green")} blabla

#     {style("OUTPUT", fg="green")} blabla
#     """,
#     short_help="template for dev",
#     no_args_is_help=True,
# )
# @click.argument("input", type=click.File("r"))
# @click.argument("output", type=click.File("w"))
# @click.option("-o", "--option", type=int, default=4, show_default=True)
# @click.option("--flag", is_flag=True)
# def template(**kwargs):
#     timer = Timer(label="template")
#     _print_kwargs(kwargs)
#     print(**kwargs)
#     timer.stop()
