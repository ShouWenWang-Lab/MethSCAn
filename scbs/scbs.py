import os
import gzip
import glob
import pandas as pd
import numpy as np
import numba
import scipy.sparse as sp_sparse
import click
from statsmodels.stats.proportion import proportion_confint
from numba import njit, prange
from sklearn.decomposition import PCA
from sklearn.preprocessing import scale
from umap import UMAP


# ignore division by 0 and division by NaN error
np.seterr(divide="ignore", invalid="ignore")


# print messages go to stderr
# output file goes to stdout (when using "-" as output file)
# that way you can pipe the output file e.g. into bedtools
def echo(*args, **kwargs):
    click.echo(*args, **kwargs, err=True)
    return


def secho(*args, **kwargs):
    click.secho(*args, **kwargs, err=True)
    return


def _get_filepath(f):
    """ returns the path of a file handle, if needed """
    if type(f) is tuple and hasattr(f[0], "name"):
        return f"{f[0].name} and {len(f) - 1} more files"
    return f.name if hasattr(f, "name") else f


def _iter_bed(file_obj, strand_col_i=None, keep_cols=False):
    is_rev_strand = False
    other_columns = False
    if strand_col_i is not None:
        strand_col_i -= 1  # CLI is 1-indexed
    for line in file_obj:
        if line.startswith("#"):
            continue  # skip comments
        values = line.strip().split("\t")
        if strand_col_i is not None:
            strand_val = values[strand_col_i]
            if strand_val == "-" or strand_val == "-1":
                is_rev_strand = True
            elif strand_val == "+" or strand_val == "1":
                is_rev_strand = False
            else:
                raise Exception(
                    f"Invalid strand column value '{strand_val}'. "
                    "Should be '+', '-', '1', or '-1'."
                )
        if keep_cols:
            other_columns = values[3:]
        # yield chrom, start, end, and whether the feature is on the minus strand
        yield values[0], int(values[1]), int(values[2]), is_rev_strand, other_columns


def _redefine_bed_regions(start, end, extend_by):
    """
    truncates or extend_bys a region to match the desired length
    """
    center = (start + end) // 2  # take center of region
    new_start = center - extend_by  # bounds = center += half region size
    new_end = center + extend_by
    return new_start, new_end


def _write_profile(
    output_file, n_meth_global, n_non_na_global, cell_names, extend_by, add_column
):
    """
    write the whole profile to a csv table in long format
    """
    output_path = _get_filepath(output_file)
    echo("Converting to long table format...")
    n_total_vals = (
        pd.DataFrame(n_non_na_global)
        .reset_index()
        .melt("index", var_name="cell", value_name="n_total")
        .get("n_total")
    )

    long_df = (
        pd.DataFrame(n_meth_global)
        .reset_index()
        .melt("index", var_name="cell", value_name="n_meth")
        .assign(cell_name=lambda x: [cell_names[c] for c in x["cell"]])
        .assign(index=lambda x: np.subtract(x["index"], extend_by))
        .assign(cell=lambda x: np.add(x["cell"], 1))
        .assign(n_total=n_total_vals)
        .loc[lambda df: df["n_total"] > 0, :]
        .assign(meth_frac=lambda x: np.divide(x["n_meth"], x["n_total"]))
        .rename(columns={"index": "position"})
    )

    echo("Calculating Agresti-Coull confidence interval...")
    ci = proportion_confint(
        long_df["n_meth"], long_df["n_total"], method="agresti_coull"
    )

    echo(f"Writing output to {output_path}...")
    long_df = long_df.assign(ci_lower=ci[0]).assign(ci_upper=ci[1])

    if add_column:
        long_df = long_df.assign(label=add_column)

    output_file.write(long_df.to_csv(index=False))
    return


def _load_chrom_mat(data_dir, chrom):
    mat_path = os.path.join(data_dir, f"{chrom}.npz")
    echo(f"loading chromosome {chrom} from {mat_path} ...")
    try:
        mat = sp_sparse.load_npz(mat_path)
    except FileNotFoundError:
        secho("Warning: ", fg="red", nl=False)
        echo(
            f"Couldn't load methylation data for chromosome {chrom} from {mat_path}. "
            f"Regions on chromosome {chrom} will not be considered."
        )
        mat = None
    return mat


def _parse_cell_names(data_dir):
    cell_names = []
    with open(os.path.join(data_dir, "column_header.txt"), "r") as col_heads:
        for line in col_heads:
            cell_names.append(line.strip())
    return cell_names


def profile(data_dir, regions, output, width, strand_column, label):
    """
    see 'scbs profile --help'
    """
    cell_names = _parse_cell_names(data_dir)
    extend_by = width // 2
    n_regions = 0  # count the total number of valid regions in the bed file
    n_empty_regions = 0  # count the number of regions that don't overlap a CpG
    observed_chroms = set()
    unknown_chroms = set()
    prev_chrom = None
    for bed_entries in _iter_bed(regions, strand_col_i=strand_column):
        chrom, start, end, is_rev_strand, _ = bed_entries
        if chrom in unknown_chroms:
            continue
        if chrom != prev_chrom:
            # we reached a new chrom, load the next matrix
            if chrom in observed_chroms:
                raise Exception(f"{_get_filepath(regions)} is not sorted!")
            mat = _load_chrom_mat(data_dir, chrom)
            if mat is None:
                unknown_chroms.add(chrom)
                continue
            echo(f"extracting methylation for regions on chromosome {chrom} ...")
            observed_chroms.add(chrom)
            if prev_chrom is None:
                # this happens at the very start, i.e. on the first chromosome
                n_cells = mat.shape[1]
                # two empty matrices will collect the number of methylated
                # CpGs and the total CpG count for every position of every
                # cell,summed over all regions
                n_meth_global = np.zeros((extend_by * 2, n_cells), dtype=np.uint32)
                n_non_na_global = np.zeros((extend_by * 2, n_cells), dtype=np.uint32)
                if strand_column:
                    n_meth_global_rev = np.zeros(
                        (extend_by * 2, n_cells), dtype=np.uint32
                    )
                    n_non_na_global_rev = np.zeros(
                        (extend_by * 2, n_cells), dtype=np.uint32
                    )
            prev_chrom = chrom

        # adding half width on both sides of the center of the region
        new_start, new_end = _redefine_bed_regions(start, end, extend_by)

        region = mat[new_start:new_end, :]
        if region.shape[0] != extend_by * 2:
            echo(
                f"skipping region {chrom}:{start}-{end} for now... "
                "out of bounds when extended... Not implemented yet!"
            )
            continue
        n_regions += 1
        if region.nnz == 0:
            # skip regions that contain no CpG
            n_empty_regions += 1
            continue

        n_meth_region = (region > 0).astype(np.uint32)
        n_non_na_region = (region != 0).astype(np.uint32)

        if not is_rev_strand:
            # handle forward regions or regions without strand info
            n_meth_global = n_meth_global + n_meth_region
            n_non_na_global = n_non_na_global + n_non_na_region
        else:
            # handle regions on the minus strand
            n_meth_global_rev = n_meth_global_rev + n_meth_region
            n_non_na_global_rev = n_non_na_global_rev + n_non_na_region

    if strand_column:
        echo("adding regions from minus strand")
        assert n_meth_global_rev.max() > 0
        assert n_non_na_global_rev.max() > 0
        n_meth_global = n_meth_global + np.flipud(n_meth_global_rev)
        n_non_na_global = n_non_na_global + np.flipud(n_non_na_global_rev)

    secho(f"\nSuccessfully profiled {n_regions} regions.", fg="green")
    echo(
        f"{n_empty_regions} of these regions "
        f"({np.divide(n_empty_regions, n_regions):.2%}) "
        f"were not observed in any cell."
    )

    if unknown_chroms:
        secho("\nWarning:", fg="red")
        echo(
            "The following chromosomes are present in "
            f"'{_get_filepath(regions)}' but not in "
            f"'{_get_filepath(data_dir)}':"
        )
        for uc in sorted(unknown_chroms):
            echo(uc)

    # write final output file of binned methylation fractions
    _write_profile(output, n_meth_global, n_non_na_global, cell_names, extend_by, label)
    return


def _get_cell_names(cov_files):
    """
    Use the file base names (without extension) as cell names
    """
    names = []
    for file_handle in cov_files:
        f = file_handle.name
        if f.lower().endswith(".gz"):
            # remove .xxx.gz
            names.append(os.path.splitext(os.path.splitext(os.path.basename(f))[0])[0])
        else:
            # remove .xxx
            names.append(os.path.splitext(os.path.basename(f))[0])
    if len(set(names)) < len(names):
        s = (
            "\n".join(names) + "\nThese sample names are not unique, "
            "check your file names again!"
        )
        raise Exception(s)
    return names


def _iterate_covfile(cov_file, c_col, p_col, m_col, u_col, coverage, sep, header):
    if cov_file.name.lower().endswith(".gz"):
        # handle gzip-compressed file
        lines = gzip.decompress(cov_file.read()).decode().strip().split("\n")
        if header:
            lines = lines[1:]
        for line in lines:
            yield _line_to_values(
                line.strip().split(sep), c_col, p_col, m_col, u_col, coverage
            )
    else:
        # handle uncompressed file
        if header:
            _ = cov_file.readline()
        for line in cov_file:
            yield _line_to_values(
                line.decode().strip().split(sep), c_col, p_col, m_col, u_col, coverage
            )


def _write_column_names(output_dir, cell_names, fname="column_header.txt"):
    """
    The column names (usually cell names) will be
    written to a separate text file
    """
    out_path = os.path.join(output_dir, fname)
    with open(out_path, "w") as col_head:
        for cell_name in cell_names:
            col_head.write(cell_name + "\n")
    return out_path


def _human_to_computer(file_format):
    if len(file_format) == 1:
        if file_format[0].lower() in ("bismarck", "bismark"):
            c_col, p_col, m_col, u_col, coverage, sep, header = (
                0,
                1,
                4,
                5,
                False,
                "\t",
                False,
            )
        elif file_format[0].lower() == "allc":
            c_col, p_col, m_col, u_col, coverage, sep, header = (
                0,
                1,
                4,
                5,
                True,
                "\t",
                True,
            )
        else:
            raise Exception(
                "Format not correct. Check --help for further information.", fg="red"
            )
    elif len(file_format) == 6:
        c_col = int(file_format[0]) - 1
        p_col = int(file_format[1]) - 1
        m_col = int(file_format[2]) - 1
        u_col = int(file_format[3][0:-1]) - 1
        info = file_format[3][-1].lower()
        if info == "c":
            coverage = True
        elif info == "m":
            coverage = False
        else:
            raise Exception(
                "Format for column with coverage/methylation must be an integer and "
                "either c for coverage or m for methylation (eg 4c)",
                fg="red",
            )
        sep = str(file_format[4])
        if sep == "\\t":
            sep = "\t"
        header = bool(int(file_format[5]))
    else:
        raise Exception(
            "Format not correct. Check --help for further information.", fg="red"
        )
    return c_col, p_col, m_col, u_col, coverage, sep, header


def _line_to_values(line, c_col, p_col, m_col, u_col, coverage):
    chrom = line[c_col]
    pos = int(line[p_col])
    n_meth = int(line[m_col])
    if coverage:
        n_unmeth = int(line[u_col]) - n_meth
    else:
        n_unmeth = int(line[u_col])
    return chrom, pos, n_meth, n_unmeth


def _dump_coo_files(fpaths, input_format, n_cells, header, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    # c_col, p_col, m_col, u_col = [f - 1 for f in input_format]
    c_col, p_col, m_col, u_col, coverage, sep, header = _human_to_computer(
        input_format.split(":")
    )
    coo_files = {}
    chrom_sizes = {}
    for cell_n, cov_file in enumerate(fpaths):
        if cell_n % 50 == 0:
            echo("{0:.2f}% done...".format(100 * cell_n / n_cells))
        for line_vals in _iterate_covfile(
            cov_file, c_col, p_col, m_col, u_col, coverage, sep, header
        ):
            chrom, genomic_pos, n_meth, n_unmeth = line_vals
            if n_meth != 0 and n_unmeth != 0:
                continue  # currently we ignore all CpGs that are not "clear"!
            meth_value = 1 if n_meth > 0 else -1
            if chrom not in coo_files:
                coo_path = os.path.join(output_dir, f"{chrom}.coo")
                coo_files[chrom] = open(coo_path, "w")
                chrom_sizes[chrom] = 0
            if genomic_pos > chrom_sizes[chrom]:
                chrom_sizes[chrom] = genomic_pos
            coo_files[chrom].write(f"{genomic_pos},{cell_n},{meth_value}\n")
    for fhandle in coo_files.values():
        # maybe somehow use try/finally or "with" to make sure
        # they're closed even when crashing
        fhandle.close()
    echo("100% done.")
    return coo_files, chrom_sizes


def _write_summary_stats(data_dir, cell_names, n_obs, n_meth):
    stats_df = pd.DataFrame(
        {
            "cell_name": cell_names,
            "n_obs": n_obs,
            "n_meth": n_meth,
            "global_meth_frac": np.divide(n_meth, n_obs),
        }
    )
    out_path = os.path.join(data_dir, "cell_stats.csv")
    with open(out_path, "w") as outfile:
        outfile.write(stats_df.to_csv(index=False))
    return out_path


def prepare(input_files, data_dir, input_format, header):
    cell_names = _get_cell_names(input_files)
    n_cells = len(cell_names)
    # we use this opportunity to count some basic summary stats
    n_obs_cell = np.zeros(n_cells, dtype=np.int64)
    n_meth_cell = np.zeros(n_cells, dtype=np.int64)

    # For each chromosome, we first make a sparse matrix in COO (coordinate)
    # format, because COO can be constructed value by value, without knowing the
    # dimensions beforehand. This means we can construct it cell by cell.
    # We dump the COO to hard disk to save RAM and then later convert each COO to a
    # more efficient format (CSR).
    echo(f"Processing {n_cells} methylation files...")
    coo_files, chrom_sizes = _dump_coo_files(
        input_files, input_format, n_cells, header, data_dir
    )
    echo(
        "\nStoring methylation data in 'compressed "
        "sparse row' (CSR) matrix format for future use."
    )

    # read each COO file and convert the matrix to CSR format.
    for chrom in coo_files.keys():
        # create empty matrix
        chrom_size = chrom_sizes[chrom]
        echo(f"Populating {chrom_size} x {n_cells} matrix for chromosome {chrom}...")
        # populate with values from temporary COO file
        coo_path = os.path.join(data_dir, f"{chrom}.coo")
        mat_path = os.path.join(data_dir, f"{chrom}.npz")
        coo = np.loadtxt(coo_path, delimiter=",")
        mat = sp_sparse.coo_matrix(
            (coo[:, 2], (coo[:, 0], coo[:, 1])),
            shape=(chrom_size + 1, n_cells),
            dtype=np.int8,
        )
        echo("Converting from COO to CSR...")
        mat = mat.tocsr()  # convert from COO to CSR format

        n_obs_cell += mat.getnnz(axis=0)
        n_meth_cell += np.ravel(np.sum(mat > 0, axis=0))

        echo(f"Writing to {mat_path} ...")
        sp_sparse.save_npz(mat_path, mat)
        os.remove(coo_path)  # delete temporary .coo file

    colname_path = _write_column_names(data_dir, cell_names)
    echo(f"\nWrote matrix column names to {colname_path}")
    stats_path = _write_summary_stats(data_dir, cell_names, n_obs_cell, n_meth_cell)
    echo(f"Wrote summary stats for each cell to {stats_path}")
    secho(
        f"\nSuccessfully stored methylation data for {n_cells} cells "
        f"with {len(coo_files.keys())} chromosomes.",
        fg="green",
    )
    return


class Smoother(object):
    def __init__(self, sparse_mat, bandwidth=1000, weigh=False):
        # create the tricube kernel
        self.hbw = bandwidth // 2
        rel_dist = np.abs((np.arange(bandwidth) - self.hbw) / self.hbw)
        self.kernel = (1 - (rel_dist ** 3)) ** 3
        # calculate (unsmoothed) methylation fraction across the chromosome
        n_obs = sparse_mat.getnnz(axis=1)
        n_meth = np.ravel(np.sum(sparse_mat > 0, axis=1))
        self.mfracs = np.divide(n_meth, n_obs)
        self.cpg_pos = (~np.isnan(self.mfracs)).nonzero()[0]
        assert n_obs.shape == n_meth.shape == self.mfracs.shape
        if weigh:
            self.weights = np.log1p(n_obs)
        self.weigh = weigh
        return

    def smooth_whole_chrom(self):
        smoothed = {}
        for i in self.cpg_pos:
            window = self.mfracs[i - self.hbw : i + self.hbw]
            nz = ~np.isnan(window)
            try:
                k = self.kernel[nz]
                if self.weigh:
                    w = self.weights[i - self.hbw : i + self.hbw][nz]
                    smooth_val = np.divide(np.sum(window[nz] * k * w), np.sum(k * w))
                else:
                    smooth_val = np.divide(np.sum(window[nz] * k), np.sum(k))
                smoothed[i] = smooth_val
            except IndexError:
                # when the smoothing bandwith is out of bounds of
                # the chromosome... needs fixing eventually
                smoothed[i] = np.nan
        return smoothed


def smooth(data_dir, bandwidth, use_weights):
    out_dir = os.path.join(data_dir, "smoothed")
    os.makedirs(out_dir, exist_ok=True)
    for mat_path in sorted(glob.glob(os.path.join(data_dir, "*.npz"))):
        chrom = os.path.basename(os.path.splitext(mat_path)[0])
        echo(f"Reading chromosome {chrom} data from {mat_path} ...")
        mat = sp_sparse.load_npz(mat_path)
        sm = Smoother(mat, bandwidth, use_weights)
        echo(f"Smoothing chromosome {chrom} ...")
        smoothed_chrom = sm.smooth_whole_chrom()
        with open(os.path.join(out_dir, f"{chrom}.csv"), "w") as smooth_out:
            for pos, smooth_val in smoothed_chrom.items():
                smooth_out.write(f"{pos},{smooth_val}\n")
    secho(f"\nSuccessfully wrote smoothed methylation data to {out_dir}.", fg="green")
    return


def _output_file_handle(raw_path):
    path = raw_path.lower()
    if path.endswith(".gz"):
        handle = gzip.open(raw_path, "wt")
    elif path.endswith(".csv"):
        handle = open(raw_path, "w")
    else:
        handle = open(raw_path + ".csv", "w")
    return handle


def _load_smoothed_chrom(data_dir, chrom):
    smoothed_path = os.path.join(data_dir, "smoothed", f"{chrom}.csv")
    if not os.path.isfile(smoothed_path):
        raise Exception(
            "Could not find smoothed methylation data for "
            f"chromosome {chrom} at {smoothed_path} . "
            "Please run 'scbs smooth' first."
        )
    typed_dict = numba.typed.Dict.empty(
        key_type=numba.types.int64,
        value_type=numba.types.float64,
    )
    with open(smoothed_path, "r") as smooth_file:
        for line in smooth_file:
            pos, smooth_val = line.strip().split(",")
            typed_dict[int(pos)] = float(smooth_val)
    return typed_dict


def matrix(
    data_dir,
    regions,
    output,
    keep_other_columns=False,
):
    output_header = [
        "chromosome",
        "start",
        "end",
        "n_sites",
        "n_cells",
        "cell_name",
        "n_meth",
        "n_obs",
        "meth_frac",
        "shrunken_residual",
    ]
    cell_names = _parse_cell_names(data_dir)
    n_regions = 0  # count the total number of valid regions in the bed file
    n_empty_regions = 0  # count the number of regions that don't overlap a CpG
    observed_chroms = set()
    unknown_chroms = set()
    prev_chrom = None

    for bed_entries in _iter_bed(regions, keep_cols=keep_other_columns):
        chrom, start, end, _, other_columns = bed_entries
        if prev_chrom is None:
            # only happens once on the very first bed entry: write header
            if other_columns and keep_other_columns:
                output_header += [f"bed_col{i + 4}" for i in range(len(other_columns))]
            output.write(",".join(output_header) + "\n")
        if chrom in unknown_chroms:
            continue
        if chrom != prev_chrom:
            # we reached a new chrom, load the next matrix
            if chrom in observed_chroms:
                raise Exception(f"{regions} is not sorted alphabetically!")
            mat = _load_chrom_mat(data_dir, chrom)
            if mat is None:
                unknown_chroms.add(chrom)
                observed_chroms.add(chrom)
                prev_chrom = chrom
                continue  # skip this region
            else:
                echo(f"extracting methylation for regions on chromosome {chrom} ...")
                smoothed_vals = _load_smoothed_chrom(data_dir, chrom)
                chrom_len, n_cells = mat.shape
                observed_chroms.add(chrom)
                prev_chrom = chrom
        # calculate methylation fraction, shrunken residuals etc. for the region:
        n_regions += 1
        n_meth, n_total, mfracs, n_obs_cpgs = _calc_region_stats(
            mat.data, mat.indices, mat.indptr, start, end, n_cells, chrom_len
        )
        nz_cells = np.nonzero(n_total > 0)[0]  # index of cells that observed the region
        n_obs_cells = nz_cells.shape[0]  # in how many cells we observed the region
        if nz_cells.size == 0:
            # skip regions that were not observed in any cell
            n_empty_regions += 1
            continue
        resid_shrunk = _calc_mean_shrunken_residuals(
            mat.data,
            mat.indices,
            mat.indptr,
            start,
            end,
            smoothed_vals,
            n_cells,
            chrom_len,
        )
        # write "count" table
        for c in nz_cells:
            out_vals = [
                chrom,
                start,
                end,
                n_obs_cpgs,
                n_obs_cells,
                cell_names[c],
                n_meth[c],
                n_total[c],
                mfracs[c],
                resid_shrunk[c],
            ]
            if keep_other_columns and other_columns:
                out_vals += other_columns
            output.write(",".join(str(v) for v in out_vals) + "\n")

    if n_regions == 0:
        raise Exception("bed file contains no regions.")
    echo(f"Profiled {n_regions} regions.\n")
    if (n_empty_regions / n_regions) > 0.5:
        secho("Warning - most regions have no coverage in any cell:", fg="red")
    echo(
        f"{n_empty_regions} regions ({n_empty_regions/n_regions:.2%}) "
        f"contained no covered methylation site."
    )
    return


@njit
def _find_peaks(smoothed_vars, swindow_centers, var_cutoff, half_bw):
    """" variance peak calling """
    peak_starts = []
    peak_ends = []
    in_peak = False
    for var, pos in zip(smoothed_vars, swindow_centers):
        if var > var_cutoff:
            if not in_peak:
                # entering new peak
                in_peak = True
                if peak_ends and pos - half_bw <= max(peak_ends):
                    # it's not really a new peak, the last peak wasn't
                    # finished, there was just a small dip...
                    peak_ends.pop()
                else:
                    peak_starts.append(pos - half_bw)
        else:
            if in_peak:
                # exiting peak
                in_peak = False
                peak_ends.append(pos + half_bw)
    if in_peak:
        peak_ends.append(pos)
    assert len(peak_starts) == len(peak_ends)
    return peak_starts, peak_ends


@njit(parallel=True)
def _move_windows(
    start,
    end,
    stepsize,
    half_bw,
    data_chrom,
    indices_chrom,
    indptr_chrom,
    smoothed_vals,
    n_cells,
    chrom_len,
):
    # shift windows along the chromosome and calculate the variance for each window.
    windows = np.arange(start, end, stepsize)
    smoothed_var = np.empty(windows.shape, dtype=np.float64)
    for i in prange(windows.shape[0]):
        pos = windows[i]
        mean_shrunk_resid = _calc_mean_shrunken_residuals(
            data_chrom,
            indices_chrom,
            indptr_chrom,
            pos - half_bw,
            pos + half_bw,
            smoothed_vals,
            n_cells,
            chrom_len,
        )
        smoothed_var[i] = np.nanvar(mean_shrunk_resid)
    return windows, smoothed_var


@njit(nogil=True)
def _calc_mean_shrunken_residuals(
    data_chrom,
    indices_chrom,
    indptr_chrom,
    start,
    end,
    smoothed_vals,
    n_cells,
    chrom_len,
    shrinkage_factor=1,
):
    shrunken_resid = np.full(n_cells, np.nan)
    if start > chrom_len:
        return shrunken_resid
    end += 1
    if end > chrom_len:
        end = chrom_len
    # slice the methylation values so that we only keep the values in the window
    data = data_chrom[indptr_chrom[start] : indptr_chrom[end]]
    if data.size == 0:
        # return NaN for regions without coverage or regions without CpGs
        return shrunken_resid
    # slice indices
    indices = indices_chrom[indptr_chrom[start] : indptr_chrom[end]]
    # slice index pointer
    indptr = indptr_chrom[start : end + 1] - indptr_chrom[start]
    indptr_diff = np.diff(indptr)

    n_obs = np.zeros(n_cells, dtype=np.int64)
    n_obs_start = np.bincount(indices)
    n_obs[0 : n_obs_start.shape[0]] = n_obs_start

    meth_sums = np.zeros(n_cells, dtype=np.int64)
    smooth_sums = np.zeros(n_cells, dtype=np.float64)
    cpg_idx = 0
    nobs_cpg = indptr_diff[cpg_idx]
    # nobs_cpg: how many of the next values correspond to the same CpG
    # e.g. a value of 3 means that the next 3 values are of the same CpG
    for i in range(data.shape[0]):
        while nobs_cpg == 0:
            cpg_idx += 1
            nobs_cpg = indptr_diff[cpg_idx]
        nobs_cpg -= 1
        cell_idx = indices[i]
        smooth_sums[cell_idx] += smoothed_vals[start + cpg_idx]
        meth_value = data[i]
        if meth_value == -1:
            continue  # skip 0 meth values when summing
        meth_sums[cell_idx] += meth_value

    for i in range(n_cells):
        if n_obs[i] > 0:
            shrunken_resid[i] = (meth_sums[i] - smooth_sums[i]) / (
                n_obs[i] + shrinkage_factor
            )
    return shrunken_resid


# currently not needed but could be useful:
# @njit
# def _count_n_cells(region_indices):
#     """
#     Count the total number of CpGs in a region, based on CSR matrix indices.
#     Only CpGs that have coverage in at least 1 cell are counted.
#     """
#     seen_cells = set()
#     n_cells = 0
#     for cell_idx in region_indices:
#         if cell_idx not in seen_cells:
#             seen_cells.add(cell_idx)
#             n_cells += 1
#     return n_cells


@njit
def _count_n_cpg(region_indptr):
    """
    Count the total number of CpGs in a region, based on CSR matrix index pointers.
    """
    prev_val = 0
    n_cpg = 0
    for val in region_indptr:
        if val != prev_val:
            n_cpg += 1
            prev_val = val
    return n_cpg


@njit
def _calc_region_stats(
    data_chrom, indices_chrom, indptr_chrom, start, end, n_cells, chrom_len
):
    n_meth = np.zeros(n_cells, dtype=np.int64)
    n_total = np.zeros(n_cells, dtype=np.int64)
    if start > chrom_len:
        n_obs_cpg = 0
    else:
        end += 1
        if end > chrom_len:
            end = chrom_len
        # slice the methylation values so that we only keep the values in the window
        data = data_chrom[indptr_chrom[start] : indptr_chrom[end]]
        if data.size > 0:
            # slice indices
            indices = indices_chrom[indptr_chrom[start] : indptr_chrom[end]]
            # slice index pointer
            indptr = indptr_chrom[start : end + 1] - indptr_chrom[start]
            n_obs_cpg = _count_n_cpg(indptr)  # total number of CpGs in the region
            for i in range(data.shape[0]):
                cell_i = indices[i]
                meth_value = data[i]
                n_total[cell_i] += 1
                if meth_value == -1:
                    continue
                n_meth[cell_i] += meth_value
    return n_meth, n_total, np.divide(n_meth, n_total), n_obs_cpg


def scan(data_dir, output, bandwidth, stepsize, var_threshold, threads=-1):
    if threads != -1:
        numba.set_num_threads(threads)
    n_threads = numba.get_num_threads()
    half_bw = bandwidth // 2
    # sort chroms by filesize. We start with largest chrom to find the var threshold
    chrom_paths = sorted(
        glob.glob(os.path.join(data_dir, "*.npz")),
        key=lambda x: os.path.getsize(x),
        reverse=True,
    )
    # will be discovered on the largest chromosome based on X% cutoff
    var_threshold_value = None
    for mat_path in chrom_paths:
        chrom = os.path.basename(os.path.splitext(mat_path)[0])
        mat = _load_chrom_mat(data_dir, chrom)
        smoothed_cpg_vals = _load_smoothed_chrom(data_dir, chrom)
        # n_obs = mat.getnnz(axis=1)
        # n_meth = np.ravel(np.sum(mat > 0, axis=1))
        # mfracs = np.divide(n_meth, n_obs)
        chrom_len, n_cells = mat.shape
        cpg_pos_chrom = np.nonzero(mat.getnnz(axis=1))[0]

        if n_threads > 1:
            echo(f"Scanning chromosome {chrom} using {n_threads} parallel threads ...")
        else:
            echo(f"Scanning chromosome {chrom} ...")
        # slide windows along the chromosome and calculate the mean
        # shrunken variance of residuals for each window.
        start = cpg_pos_chrom[0] + half_bw + 1
        end = cpg_pos_chrom[-1] - half_bw - 1
        genomic_positions, window_variances = _move_windows(
            start,
            end,
            stepsize,
            half_bw,
            mat.data,
            mat.indices,
            mat.indptr,
            smoothed_cpg_vals,
            n_cells,
            chrom_len,
        )

        if var_threshold_value is None:
            # this is the first=biggest chrom, so let's find our variance threshold here
            var_threshold_value = np.nanquantile(window_variances, 1 - var_threshold)
            echo(f"Determined the variance threshold of {var_threshold_value}.")

        peak_starts, peak_ends = _find_peaks(
            window_variances, genomic_positions, var_threshold_value, half_bw
        )

        for ps, pe in zip(peak_starts, peak_ends):
            peak_var = np.nanvar(
                _calc_mean_shrunken_residuals(
                    mat.data,
                    mat.indices,
                    mat.indptr,
                    ps,
                    pe,
                    smoothed_cpg_vals,
                    n_cells,
                    chrom_len,
                )
            )
            bed_entry = f"{chrom}\t{ps}\t{pe}\t{peak_var}\n"
            output.write(bed_entry)
        if len(peak_starts) > 0:
            secho(
                f"Found {len(peak_starts)} variable regions on chromosome {chrom}.",
                fg="green",
            )
        else:
            secho(
                f"Found no variable regions on chromosome {chrom}.",
                fg="red",
            )
    return


def imputing_pca(
    X, n_components=10, n_iterations=10, scale_features=True, center_features=True
):
    # center and scale features
    X = scale(X, axis=0, with_mean=center_features, with_std=scale_features)
    # for each set of predicted values, we calculated how similar it is to the values
    # we predicted in the previous iteration, so that we can roughly see when our
    # prediction converges
    dist = np.full(n_iterations, fill_value=np.nan)
    # varexpl = np.full(n_iterations, fill_value=np.nan)
    nan_positions = np.isnan(X)
    X[nan_positions] = 0  # zero is our first guess for all missing values
    # start iterative imputation
    for i in range(n_iterations):
        echo(f"PCA iteration {i + 1}...")
        previous_guess = X[nan_positions]  # what we imputed in the previous iteration
        # PCA on the imputed matrix
        pca = PCA(n_components=n_components)
        pca.fit(X)
        # impute missing values with PCA
        new_guess = (pca.inverse_transform(pca.transform(X)))[nan_positions]
        X[nan_positions] = new_guess
        # compare our new imputed values to the ones from the previous round
        dist[i] = np.mean((previous_guess - new_guess) ** 2)
        # varexpl[i] = np.sum(pca.explained_variance_ratio_)
    pca.prediction_dist_iter = dist  # distance between predicted values
    # pca.total_var_exp_iter = varexpl
    pca.X_imputed = X
    return pca


def reduce(
    matrix,  # filepath to a matrix produced by scbs matrix OR pandas DataFrame
    value_column="shrunken_residual",  # the name of the column containing the methylation values
    center_cells=False,  # subtract the mean methylation from each cell? (should be useful for GpC accessibility data)
    max_na_region=.7,  # maximum allowed fraction of missing values for each region. Set this to 1 to prevent filtering.
    max_na_cell=.95,  # maximum allowed fraction of missing values for each cell (note that this threshold is applied after regions were filtered)
    n_pc=10,  # number of principal components to compute
    n_iterations=10,  # number of iterations for PCA imputation
    n_neighbors=20,  # a umap parameter
    min_dist=0.1,  # a umap parameter
):
    """
    Takes the output of 'scbs matrix' and reduces it to fewer dimensions, first by
    PCA and then by UMAP.
    """
    if isinstance(matrix, str):
        df = pd.read_csv(matrix, header=0)
    elif isinstance(matrix, pd.core.frame.DataFrame):
        df = matrix
    else:
        raise Exception("'matrix' must be either a path or a pandas DataFrame.")
    # make a proper matrix (cell x region)
    echo("Converting long matrix to wide matrix...")
    df_wide = (
        df.assign(
            region=df.apply(
                lambda x: f"{x['chromosome']}:{x['start']}-{x['end']}", axis=1
            )
        )
        .loc[:, ["cell_name", "region", value_column]]
        .pivot(index="cell_name", columns="region", values=value_column)
    )
    X = np.array(df_wide)
    Xdim_old = X.shape
    # filter regions that were not observed in many cells
    na_frac_region = np.sum(np.isnan(X), axis=0) / X.shape[0]
    X = X[:, na_frac_region <= max_na_region]
    echo(f"filtered {Xdim_old[1] - X.shape[1]} of {Xdim_old[1]} regions.")
    # filter cells that did not observe many regions
    na_frac_cell = np.sum(np.isnan(X), axis=1) / X.shape[1]
    X = X[na_frac_cell <= max_na_cell, :]
    echo(f"filtered {Xdim_old[0] - X.shape[0]} of {Xdim_old[0]} cells.")
    cell_names = df_wide.index[na_frac_cell <= max_na_cell]
    percent_missing = na_frac_cell[na_frac_cell <= max_na_cell] * 100
    # optionally: for each value, subtract the mean methylation of that cell
    # this is mostly for GpC-accessibility data since some cells may receive more
    # methylase than others
    if center_cells:
        X = scale(X, axis=1, with_mean=True, with_std=False)
        echo("centered cells.")
    # run our modified PCA
    echo(f"running modified PCA ({n_pc=}, {n_iterations=})...")
    pca = imputing_pca(X, n_components=n_pc, n_iterations=n_iterations)
    X_pca_reduced = pca.transform(pca.X_imputed)
    echo(f"running UMAP ({n_neighbors=}, {min_dist=})...")
    reducer = UMAP(n_neighbors=n_neighbors, min_dist=min_dist)
    X_umap_reduced = reducer.fit_transform(X_pca_reduced)
    # generate output table as pandas df
    col_names = ["UMAP" + str(i + 1) for i in range(X_umap_reduced.shape[1])] + [
        "PC" + str(i + 1) for i in range(X_pca_reduced.shape[1])
    ]
    out_df = pd.DataFrame(
        data=np.concatenate((X_umap_reduced, X_pca_reduced), axis=1),
        index=cell_names,
        columns=col_names,
    )
    out_df["percent_missing"] = percent_missing
    return out_df, pca
