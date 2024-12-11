#!/usr/bin/env python3
"""
dN/dS Analysis Script using PAML's CODEML

This script calculates pairwise dN/dS values using PAML's CODEML program.
It processes input files that contain nucleotide sequences. Each line corresponds
to a single sample and its sequence, with the sample name ending in "_0" or "_1"
immediately followed by the sequence, without any intervening space.

For example:
AMR_CLM_HG01352_0AAGAAGTAC...

No header lines are assumed. The code extracts the sample name by identifying
the "_0" or "_1" suffix and then takes the rest of the line as the sequence.

Usage:
    python3 dnds.py --phy_dir PATH_TO_PHY_FILES --output_dir OUTPUT_DIRECTORY --codeml_path PATH_TO_CODEML
"""

import os
import sys
import glob
import subprocess
import multiprocessing
import psutil
from itertools import combinations
import pandas as pd
import numpy as np
import shutil
import re
import argparse
import time
import logging
import pickle

COMPARE_BETWEEN_GROUPS = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('dnds_analysis.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

GLOBAL_INVALID_SEQS = 0
GLOBAL_DUPLICATES = 0
GLOBAL_TOTAL_SEQS = 0
GLOBAL_TOTAL_CDS = 0
GLOBAL_TOTAL_COMPARISONS = 0

def validate_sequence(seq):
    global GLOBAL_INVALID_SEQS
    if len(seq) % 3 != 0:
        GLOBAL_INVALID_SEQS += 1
        return None
    valid_bases = set('ATCGNatcgn-')
    if not set(seq).issubset(valid_bases):
        invalid_chars = set(seq) - valid_bases
        logging.warning(f"Invalid chars {invalid_chars} found. Skipping.")
        GLOBAL_INVALID_SEQS += 1
        return None
    return seq.upper()

def extract_group_from_sample(sample_name):
    match = re.search(r'_(0|1)$', sample_name)
    if match:
        return int(match.group(1))
    else:
        logging.warning(f"Group suffix not found in {sample_name}")
        return None

def create_paml_ctl(seqfile, outfile, working_dir):
    ctl_content = f"""
      seqfile = {seqfile}
      treefile = tree.txt
      outfile = {outfile}
      noisy = 0
      verbose = 0
      runmode = -2
      seqtype = 1
      CodonFreq = 2
      model = 0
      NSsites = 0
      icode = 0
      fix_kappa = 0
      kappa = 2.0
      fix_omega = 0
      omega = 1.0
      fix_alpha = 1
      alpha = 0.0
      getSE = 0
      RateAncestor = 0
      cleandata = 1
    """
    ctl_path = os.path.join(working_dir, 'codeml.ctl')
    with open(ctl_path, 'w') as f:
        f.write(ctl_content)
    return ctl_path

def run_codeml(ctl_path, working_dir, codeml_path):
    try:
        process = subprocess.Popen(
            [codeml_path],
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = process.communicate(timeout=300)
        if process.returncode != 0:
            abs_codeml_path = os.path.abspath(os.path.join(working_dir, codeml_path))
            logging.error(f"CODEML failed at {abs_codeml_path}: {stderr.decode('utf-8')}")
            return False
        return True
    except subprocess.TimeoutExpired:
        process.kill()
        logging.error("CODEML timed out.")
        return False
    except Exception as e:
        logging.error(f"Error running CODEML: {e}")
        return False

def parse_codeml_output(outfile_dir):
    rst_file = os.path.join(outfile_dir, 'rst')
    if not os.path.exists(rst_file):
        logging.error(f"CODEML output file not found: {rst_file}")
        return None, None, None
    try:
        with open(rst_file, 'r') as f:
            content = f.read()
        pattern = re.compile(
            r"t=\s*[\d\.]+\s+S=\s*([\d\.]+)\s+N=\s*([\d\.]+)\s+"
            r"dN/dS=\s*([\d\.]+)\s+dN=\s*([\d\.]+)\s+dS=\s*([\d\.]+)"
        )
        match = pattern.search(content)
        if match:
            dN = float(match.group(4))
            dS = float(match.group(5))
            omega = float(match.group(3))
            return dN, dS, omega
        else:
            logging.error("Could not parse CODEML output.")
            return None, None, None
    except Exception as e:
        logging.error(f"Error parsing CODEML output: {e}")
        return None, None, None

def get_safe_process_count():
    total_cpus = multiprocessing.cpu_count()
    mem = psutil.virtual_memory()
    safe_processes = max(1, min(total_cpus // 2, int(mem.available / (2 * 1024**3))))
    return safe_processes

def parse_phy_file(filepath):
    global GLOBAL_DUPLICATES, GLOBAL_TOTAL_SEQS
    sequences = {}
    duplicates_found = False
    if not os.path.exists(filepath):
        logging.error(f"File not found: {filepath}")
        return {}, False

    with open(filepath, 'r') as file:
        lines = file.readlines()

    if not lines:
        logging.error(f"PHYLIP file is empty: {filepath}")
        return {}, False

    # We assume no header. Each line should have a sample name ending in _0 or _1,
    # followed immediately by the sequence. We use a regex to identify the sample name.
    # The pattern: (.*_(0|1))([ATCGNatcgn-]+)
    # We split at the last occurrence of _0 or _1 before the sequence starts.
    pattern = re.compile(r'(?P<name>.+_(0|1))(?P<seq>[ATCGNatcgn-]+)', re.IGNORECASE)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        match = pattern.match(line)
        if not match:
            logging.error(f"Line does not match expected format in {filepath}: {line}")
            continue

        sample_name = match.group('name')
        sequence = match.group('seq')

        validated_seq = validate_sequence(sequence)
        if validated_seq:
            GLOBAL_TOTAL_SEQS += 1
            if sample_name in sequences:
                duplicates_found = True
                base_name = sample_name[:2] + sample_name[3:]
                dup_count = sum(1 for s in sequences.keys() if s[:2] + s[3:] == base_name)
                new_name = sample_name[:2] + str(dup_count) + sample_name[3:]
                logging.info(f"Duplicate {sample_name} -> {new_name}")
                sequences[new_name] = validated_seq
                GLOBAL_DUPLICATES += 1
            else:
                sequences[sample_name] = validated_seq

    return sequences, duplicates_found

def load_cache(cache_file):
    if os.path.exists(cache_file):
        with open(cache_file, 'rb') as f:
            cache = pickle.load(f)
        logging.info(f"Cache loaded from {cache_file}")
    else:
        cache = {}
    return cache

def save_cache(cache_file, cache_data):
    with open(cache_file, 'wb') as f:
        pickle.dump(cache_data, f)
    logging.info(f"Cache saved to {cache_file}")

def process_pair(args):
    pair, sequences, sample_groups, cds_id, codeml_path, temp_dir, cache = args
    seq1_name, seq2_name = pair
    cache_key = (cds_id, seq1_name, seq2_name, COMPARE_BETWEEN_GROUPS)
    if cache_key in cache:
        return cache[cache_key]

    if seq1_name not in sequences or seq2_name not in sequences:
        logging.error(f"Sequences missing: {seq1_name}, {seq2_name}")
        return None

    group1 = sample_groups.get(seq1_name)
    group2 = sample_groups.get(seq2_name)

    if not COMPARE_BETWEEN_GROUPS and group1 != group2:
        return None

    if sequences[seq1_name] == sequences[seq2_name]:
        # Identical seqs
        result = (seq1_name, seq2_name, group1, group2, 0.0, 0.0, -1.0, cds_id)
        cache[cache_key] = result
        return result

    working_dir = os.path.join(temp_dir, f'{seq1_name}_{seq2_name}')
    if not os.path.exists(working_dir):
        os.makedirs(working_dir)

    seqfile = os.path.join(working_dir, 'seqfile.phy')
    with open(seqfile, 'w') as f:
        f.write(f" 2 {len(sequences[seq1_name])}\n")
        f.write(f"{seq1_name} {sequences[seq1_name]}\n")
        f.write(f"{seq2_name} {sequences[seq2_name]}\n")

    treefile = os.path.join(working_dir, 'tree.txt')
    with open(treefile, 'w') as f:
        f.write(f"({seq1_name},{seq2_name});\n")

    ctl_path = create_paml_ctl(seqfile, 'mlc', working_dir)
    success = run_codeml(ctl_path, working_dir, codeml_path)
    if not success:
        result = (seq1_name, seq2_name, group1, group2, np.nan, np.nan, np.nan, cds_id)
        cache[cache_key] = result
        return result

    dn, ds, omega = parse_codeml_output(working_dir)
    if omega is None:
        omega = np.nan
    result = (seq1_name, seq2_name, group1, group2, dn, ds, omega, cds_id)
    cache[cache_key] = result
    return result

def estimate_total_comparisons(phy_dir):
    global GLOBAL_TOTAL_CDS, GLOBAL_TOTAL_COMPARISONS
    phy_files = glob.glob(os.path.join(phy_dir, '*.phy'))
    total_comparisons = 0
    for phy_file in phy_files:
        sequences, duplicates = parse_phy_file(phy_file)
        if not sequences:
            continue
        sample_groups = {}
        skip_file = False
        for s in sequences.keys():
            g = extract_group_from_sample(s)
            if g is None:
                skip_file = True
                break
            sample_groups[s] = g
        if skip_file:
            continue

        if COMPARE_BETWEEN_GROUPS:
            pairs = list(combinations(sequences.keys(), 2))
        else:
            group0_samples = [s for s, gg in sample_groups.items() if gg == 0]
            group1_samples = [s for s, gg in sample_groups.items() if gg == 1]
            pairs = list(combinations(group0_samples, 2)) + list(combinations(group1_samples, 2))

        if pairs:
            GLOBAL_TOTAL_CDS += 1
        total_comparisons += len(pairs)

    GLOBAL_TOTAL_COMPARISONS = total_comparisons

def process_phy_file(args):
    phy_file, output_dir, codeml_path, total_files, file_index, cache = args
    start_time = time.time()
    phy_filename = os.path.basename(phy_file)

    basename = phy_filename.replace('.phy', '')
    m = re.match(r'group_\d+_chr_(\w+)_start_(\d+)_end_(\d+)', basename)
    chrom, start_str, end_str = m.groups()
    start = int(start_str)
    end = int(end_str)
    cds_id = basename

    mode_suffix = "_all" if COMPARE_BETWEEN_GROUPS else ""
    output_csv = os.path.join(output_dir, f'{cds_id}{mode_suffix}.csv')
    haplotype_output_csv = os.path.join(output_dir, f'{cds_id}{mode_suffix}_haplotype_stats.csv')

    if os.path.exists(output_csv) and os.path.exists(haplotype_output_csv):
        logging.info(f"Results exist for {cds_id}. Skipping.")
        return haplotype_output_csv

    sequences, has_duplicates = parse_phy_file(phy_file)
    if not sequences:
        logging.error(f"No valid sequences in {phy_file}. Skipping.")
        return None

    if has_duplicates:
        print(f"CLEARING CACHE for {os.path.basename(phy_file)} due to duplicates")
        logging.info(f"Clearing cache for {os.path.basename(phy_file)}")
        keys_to_remove = [k for k in cache.keys() if k[0] == cds_id]
        for k in keys_to_remove:
            del cache[k]

    sample_groups = {}
    for sample_name in sequences.keys():
        g = extract_group_from_sample(sample_name)
        if g is None:
            logging.error(f"No group for {sample_name}. Skipping file.")
            return None
        sample_groups[sample_name] = g

    if COMPARE_BETWEEN_GROUPS:
        all_samples = list(sample_groups.keys())
        pairs = list(combinations(all_samples, 2))
    else:
        group0_samples = [s for s, gg in sample_groups.items() if gg == 0]
        group1_samples = [s for s, gg in sample_groups.items() if gg == 1]
        pairs = list(combinations(group0_samples, 2)) + list(combinations(group1_samples, 2))

    if not pairs:
        logging.error(f"No pairs in {phy_file}.")
        return None

    temp_dir = os.path.join(output_dir, 'temp', cds_id)
    os.makedirs(temp_dir, exist_ok=True)

    pool_args = [(pair, sequences, sample_groups, cds_id, codeml_path, temp_dir, cache) for pair in pairs]

    num_processes = get_safe_process_count()
    results = []
    completed = 0
    total_pairs = len(pool_args)

    def on_result(res):
        nonlocal completed
        if res is not None:
            results.append(res)
        completed += 1
        pct = (completed / total_pairs) * 100
        logging.info(f"Progress: {completed}/{total_pairs} pairs ({pct:.2f}%)")

    with multiprocessing.Pool(processes=num_processes) as pool:
        for r in pool.imap_unordered(process_pair, pool_args):
            on_result(r)

    df = pd.DataFrame(results, columns=['Seq1','Seq2','Group1','Group2','dN','dS','omega','CDS'])
    df.to_csv(output_csv, index=False)

    hap_stats = []
    for sample in sequences.keys():
        sample_df = df[(df['Seq1'] == sample) | (df['Seq2'] == sample)]
        omega_vals = sample_df['omega'].dropna()
        omega_vals = omega_vals[~omega_vals.isin([-1,99])]
        if not omega_vals.empty:
            mean_omega = omega_vals.mean()
            median_omega = omega_vals.median()
        else:
            mean_omega = np.nan
            median_omega = np.nan
        hap_stats.append({
            'Haplotype': sample,
            'Group': sample_groups[sample],
            'CDS': cds_id,
            'Mean_dNdS': mean_omega,
            'Median_dNdS': median_omega,
            'Num_Comparisons': len(omega_vals)
        })
    hap_df = pd.DataFrame(hap_stats)
    hap_df.to_csv(haplotype_output_csv, index=False)

    shutil.rmtree(temp_dir, ignore_errors=True)
    end_time = time.time()
    logging.info(f"Processed {phy_file} in {end_time-start_time:.2f}s")
    return haplotype_output_csv

def main():
    global GLOBAL_INVALID_SEQS, GLOBAL_DUPLICATES, GLOBAL_TOTAL_SEQS, GLOBAL_TOTAL_CDS, GLOBAL_TOTAL_COMPARISONS
    parser = argparse.ArgumentParser(description="Calculate pairwise dN/dS using PAML.")
    parser.add_argument('--phy_dir', type=str, default='.', help='Directory containing .phy files.')
    parser.add_argument('--output_dir', type=str, default='paml_output', help='Directory to store output files.')
    parser.add_argument('--codeml_path', type=str, default='../paml/bin/codeml', help='Path to codeml executable.')
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    cache_file = os.path.join(args.output_dir, 'results_cache.pkl')
    cache = load_cache(cache_file)

    estimate_total_comparisons(args.phy_dir)
    phy_files = glob.glob(os.path.join(args.phy_dir, '*.phy'))
    total_files = len(phy_files)

    logging.info("=== START OF RUN SUMMARY ===")
    logging.info(f"Total PHYLIP files: {total_files}")
    logging.info(f"Total sequences encountered: {GLOBAL_TOTAL_SEQS}")
    logging.info(f"Invalid sequences: {GLOBAL_INVALID_SEQS}")
    logging.info(f"Duplicates: {GLOBAL_DUPLICATES}")
    logging.info(f"Total CDS: {GLOBAL_TOTAL_CDS}")
    logging.info(f"Expected comparisons: {GLOBAL_TOTAL_COMPARISONS}")

    cached_results_count = len(cache)
    remaining = GLOBAL_TOTAL_COMPARISONS - cached_results_count
    logging.info(f"Cache: {cached_results_count} results. {remaining} remain.")

    work_args = []
    for phy_file in phy_files:
        phy_filename = os.path.basename(phy_file)
        cds_id = phy_filename.replace('.phy','')
        mode_suffix = "_all" if COMPARE_BETWEEN_GROUPS else ""
        output_csv = os.path.join(args.output_dir, f'{cds_id}{mode_suffix}.csv')
        haplotype_output_csv = os.path.join(args.output_dir, f'{cds_id}{mode_suffix}_haplotype_stats.csv')
        if os.path.exists(output_csv) and os.path.exists(haplotype_output_csv):
            logging.info(f"Skipping {phy_file}, output exists.")
            continue
        work_args.append((phy_file, args.output_dir, args.codeml_path, total_files, len(work_args)+1, cache))

    total_new_files = len(work_args)
    completed_comparisons = cached_results_count
    start_time = time.time()

    def print_eta(completed, total, start):
        elapsed = time.time()-start
        if elapsed>0 and completed>0:
            rate = completed/elapsed
            remain = total-completed
            if rate>0:
                eta_sec=remain/rate
                hrs=eta_sec/3600
                logging.info(f"Progress: {completed}/{total} comps. ETA: {hrs:.2f}h")
            else:
                logging.info(f"Progress: {completed}/{total}, ETA:N/A")
        else:
            logging.info(f"Progress: {completed}/{total}, ETA:N/A")

    for idx, arg_t in enumerate(work_args, 1):
        phy_file = arg_t[0]
        logging.info(f"Processing file {idx}/{total_new_files}: {phy_file}")
        hap_file = process_phy_file(arg_t)
        old_size = len(cache)
        save_cache(cache_file, cache)
        new_size = len(cache)
        newly_done = new_size - old_size
        completed_comparisons += newly_done
        percent = (completed_comparisons/GLOBAL_TOTAL_COMPARISONS*100) if GLOBAL_TOTAL_COMPARISONS>0 else 0
        logging.info(f"Overall: {completed_comparisons}/{GLOBAL_TOTAL_COMPARISONS} comps ({percent:.2f}%)")
        print_eta(completed_comparisons, GLOBAL_TOTAL_COMPARISONS, start_time)

    end_time = time.time()
    final_invalid_pct = (GLOBAL_INVALID_SEQS/GLOBAL_TOTAL_SEQS*100) if GLOBAL_TOTAL_SEQS>0 else 0
    logging.info("=== END OF RUN SUMMARY ===")
    logging.info(f"Total PHYLIP: {total_files}")
    logging.info(f"Total seq: {GLOBAL_TOTAL_SEQS}")
    logging.info(f"Invalid seq: {GLOBAL_INVALID_SEQS} ({final_invalid_pct:.2f}%)")
    logging.info(f"Duplicates: {GLOBAL_DUPLICATES}")
    logging.info(f"Total CDS: {GLOBAL_TOTAL_CDS}")
    logging.info(f"Expected comps: {GLOBAL_TOTAL_COMPARISONS}")
    logging.info(f"Completed comps: {completed_comparisons}")
    logging.info(f"Total time: {(end_time-start_time)/60:.2f} min")

    logging.info("dN/dS analysis done.")

if __name__ == '__main__':
    main()
