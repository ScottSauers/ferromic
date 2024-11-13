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
import hashlib
from scipy.stats import mannwhitneyu, wilcoxon

# ----------------------------
# 1. Setup Logging
# ----------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('dnds_analysis.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

# ----------------------------
# 2. Utility Functions
# ----------------------------

def validate_sequence(seq):
    """Validate that sequence is codon-aligned and contains only valid nucleotides."""
    if len(seq) % 3 != 0:
        logging.warning(f"Skipping sequence of length {len(seq)}: not divisible by 3")
        return None

    valid_bases = set('ATCGNatcgn-')
    invalid_chars = set(seq) - valid_bases
    if invalid_chars:
        logging.warning(f"Skipping sequence with invalid nucleotides: {invalid_chars}")
        return None

    return seq

def generate_checksum(full_name):
    """Generate a 3-character checksum from the full sample name."""
    hash_object = hashlib.md5(full_name.encode())
    checksum = hash_object.hexdigest()[:3].upper()
    return checksum

def extract_group_from_sample(sample_name):
    """Extract the group from a sample name, expecting it to end with _0 or _1."""
    sample_name = sample_name.strip()
    match = re.search(r'_(0|1)$', sample_name)
    if match:
        return int(match.group(1))
    else:
        logging.warning(f"Could not extract group from sample name: {sample_name}")
        return None

def create_paml_ctl(seqfile, outfile, working_dir):
    """Create CODEML control file with proper formatting."""
    ctl_content = f"""      seqfile = {seqfile}
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
    """Run CODEML and handle any prompts."""
    logging.info(f"\n=== Running CODEML in {working_dir} ===")
    start_time = time.time()
    
    try:
        # Use subprocess to run CODEML, send newline to bypass 'Press Enter'
        process = subprocess.Popen(
            [codeml_path],
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE
        )

        stdout, stderr = process.communicate(input=b'\n', timeout=3600)
        runtime = time.time() - start_time
        logging.info(f"CODEML runtime: {runtime:.2f}s")
        
        if process.returncode != 0:
            logging.error(f"CODEML error: {stderr.decode()}")
            return False
            
        return True
        
    except subprocess.TimeoutExpired:
        logging.error("CODEML execution timed out.")
        process.kill()
        return False
    except Exception as e:
        logging.error(f"ERROR running CODEML: {str(e)}")
        return False

def parse_codeml_output(outfile_dir):
    """
    Parse CODEML output files to extract dN, dS, and omega.
    Returns tuple: (dN, dS, omega)
    """
    logging.info(f"\n=== Parsing CODEML output in {outfile_dir} ===")
    
    # Initialize results
    results = {
        'dN': None,
        'dS': None,
        'omega': None,
        'N': None,
        'S': None,
        'lnL': None
    }
    
    # Attempt to parse RST file first
    rst_path = os.path.join(outfile_dir, 'rst')
    if os.path.exists(rst_path):
        logging.info("Found RST file, attempting to parse...")
        try:
            with open(rst_path, 'r') as f:
                content = f.read()
                
            # Example regex to extract dN, dS, omega from RST
            # This needs to be adjusted based on the actual RST file format
            pairwise_match = re.search(
                r'(\d+)\s+(\d+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)',
                content
            )
            
            if pairwise_match:
                # Extract values assuming correct group indices
                results['N'] = float(pairwise_match.group(3))
                results['S'] = float(pairwise_match.group(4))
                results['dN'] = float(pairwise_match.group(5))
                results['dS'] = float(pairwise_match.group(6))
                results['omega'] = float(pairwise_match.group(7))
                results['lnL'] = float(pairwise_match.group(8))
                
                logging.info(f"Successfully parsed RST file:")
                logging.info(f"  N sites: {results['N']:.1f}")
                logging.info(f"  S sites: {results['S']:.1f}")
                logging.info(f"  dN: {results['dN']:.6f}")
                logging.info(f"  dS: {results['dS']:.6f}")
                logging.info(f"  dN/dS: {results['omega']:.6f}")
                logging.info(f"  lnL: {results['lnL']:.6f}")
                
                return (results['dN'], results['dS'], results['omega'])
            else:
                logging.warning("Could not find pairwise comparison section in RST file")
        
        except Exception as e:
            logging.error(f"Error parsing RST file: {str(e)}")
    else:
        logging.warning("RST file not found")
    
    # Fallback to parsing results.txt
    logging.info("Attempting to parse results.txt as fallback...")
    results_path = os.path.join(outfile_dir, 'results.txt')
    
    if not os.path.exists(results_path):
        logging.error("results.txt not found!")
        return (None, None, None)
        
    try:
        with open(results_path, 'r') as f:
            content = f.read()
            
        # Example regex to extract dN, dS, omega from results.txt
        # This needs to be adjusted based on the actual results.txt file format
        ml_match = re.search(
            r't=\s*[\d\.]+\s+S=\s*([\d\.]+)\s+N=\s*([\d\.]+)\s+dN/dS=\s*([\d\.]+)\s+dN\s*=\s*([\d\.]+)\s+dS\s*=\s*([\d\.]+)',
            content
        )
        
        if ml_match:
            results['S'] = float(ml_match.group(1))
            results['N'] = float(ml_match.group(2))
            results['omega'] = float(ml_match.group(3))
            results['dN'] = float(ml_match.group(4))
            results['dS'] = float(ml_match.group(5))
            
            logging.info(f"Successfully parsed results.txt:")
            logging.info(f"  N sites: {results['N']:.1f}")
            logging.info(f"  S sites: {results['S']:.1f}")
            logging.info(f"  dN: {results['dN']:.6f}")
            logging.info(f"  dS: {results['dS']:.6f}")
            logging.info(f"  dN/dS: {results['omega']:.6f}")
            
            return (results['dN'], results['dS'], results['omega'])
            
        else:
            logging.error("Could not find ML output section in results.txt")
            
    except Exception as e:
        logging.error(f"Error parsing results.txt: {str(e)}")
    
    # Last resort: Parsing 2ML.* files
    logging.info("Checking 2ML.* files as last resort...")
    try:
        with open(os.path.join(outfile_dir, '2ML.dN'), 'r') as f:
            lines = f.readlines()
            if len(lines) >= 3:
                results['dN'] = float(lines[2].strip().split()[-1])
            else:
                logging.warning("2ML.dN file does not have enough lines.")
        
        with open(os.path.join(outfile_dir, '2ML.dS'), 'r') as f:
            lines = f.readlines()
            if len(lines) >= 3:
                results['dS'] = float(lines[2].strip().split()[-1])
            else:
                logging.warning("2ML.dS file does not have enough lines.")
        
        # Compute omega if dS is not zero
        if results['dS'] and results['dS'] != 0:
            results['omega'] = results['dN'] / results['dS']
        else:
            results['omega'] = np.nan  # Assign NaN if dS is zero or missing
        
        logging.info(f"Successfully parsed 2ML.* files:")
        logging.info(f"  dN: {results['dN']:.6f}")
        logging.info(f"  dS: {results['dS']:.6f}")
        logging.info(f"  dN/dS: {results['omega'] if not pd.isna(results['omega']) else 'NaN'}")
        
        return (results['dN'], results['dS'], results['omega'])
        
    except Exception as e:
        logging.error(f"Error parsing 2ML.* files: {str(e)}")
    
    logging.error("Failed to parse CODEML output from any available file")
    return (None, None, None)

def get_safe_process_count():
    """Determine a safe number of parallel processes based on CPU and memory."""
    total_cpus = multiprocessing.cpu_count()
    mem = psutil.virtual_memory()
    
    # Conservative allocation - use at most 25% of CPUs and 4GB per process
    safe_cpu_count = max(1, min(total_cpus // 4, 8))
    mem_based_count = max(1, int(mem.available / (4 * 1024 * 1024 * 1024)))
    
    process_count = min(safe_cpu_count, mem_based_count)
    logging.info(f"System resources: {total_cpus} CPUs, {mem.available/1024/1024/1024:.1f}GB free RAM")
    logging.info(f"Using {process_count} parallel processes")
    
    return process_count

# ----------------------------
# 3. Core Processing Functions
# ----------------------------

def process_pair(args):
    """
    Process a single pair of sequences.
    Filters out omega values of -1 and 99 before returning.
    """
    pair, sequences, sample_groups, cds_id, codeml_path, temp_dir = args
    seq1_name, seq2_name = pair
    
    # Validate that sequences are from the same group
    group1 = sample_groups.get(seq1_name)
    group2 = sample_groups.get(seq2_name)
    
    if group1 != group2:
        logging.error(f"CRITICAL ERROR: Attempted cross-group comparison: {seq1_name} (Group {group1}) vs {seq2_name} (Group {group2})")
        return None
        
    # Check if sequences are identical
    if sequences[seq1_name] == sequences[seq2_name]:
        logging.info(f"Sequences {seq1_name} and {seq2_name} from group {group1} are identical - skipping PAML")
        return (
            seq1_name.strip(),
            seq2_name.strip(),
            group1,
            group1,  # Explicitly use same group
            0.0,     # dN = 0 for identical sequences
            0.0,     # dS = 0 for identical sequences
            -1.0,    # omega = -1 to indicate identical sequences
            cds_id
        )
    
    # Create unique working directory for non-identical sequences
    timestamp = int(time.time())
    working_dir = os.path.join(temp_dir, f'temp_group{group1}_{seq1_name}_{seq2_name}_{timestamp}')
    os.makedirs(working_dir, exist_ok=True)

    # Write PHYLIP file - exactly 10 chars with two spaces after name
    phy_path = os.path.join(working_dir, 'temp.phy')
    with open(phy_path, 'w') as f:
        seq_len = len(sequences[seq1_name])
        f.write(f" 2 {seq_len}\n")
        f.write(f"{seq1_name[:10].ljust(10)}  {sequences[seq1_name]}\n")
        f.write(f"{seq2_name[:10].ljust(10)}  {sequences[seq2_name]}\n")

    # Create control file
    ctl_path = create_paml_ctl('temp.phy', 'results.txt', working_dir)
    
    # Create empty tree file
    tree_path = os.path.join(working_dir, 'tree.txt')
    with open(tree_path, 'w') as f:
        f.write('')
    
    # Run CODEML
    success = run_codeml(ctl_path, working_dir, codeml_path)

    # Parse results and return the 8-tuple
    if success:
        dn, ds, omega = parse_codeml_output(working_dir)
        # Ensure omega is a float or np.nan
        if omega is None or not isinstance(omega, (int, float)):
            omega = np.nan
        return (
            seq1_name.strip(),
            seq2_name.strip(), 
            group1,
            group1,  # Explicitly use same group
            dn,
            ds,
            omega,
            cds_id
        )
    else:
        return (
            seq1_name.strip(),
            seq2_name.strip(),
            group1,
            group1,  # Explicitly use same group
            np.nan,  # dN
            np.nan,  # dS
            np.nan,  # omega
            cds_id
        )

def process_phy_file(args):
    """
    Process a single PHYLIP file:
    - Parse sequences
    - Generate within-group pairs
    - Run CODEML on each pair
    - Aggregate haplotype statistics
    """
    phy_file, output_dir, codeml_path, total_files, file_index = args

    logging.info(f"\n====== Processing file {file_index}/{total_files}: {phy_file} ======")
    start_time = time.time()

    # Parse filename and extract CDS info
    phy_filename = os.path.basename(phy_file)
    match = re.match(r'group_(\d+)_chr_(.+)_start_(\d+)_end_(\d+)\.phy', phy_filename)
    if match:
        group = int(match.group(1))
        chr_num = match.group(2)
        start = match.group(3)
        end = match.group(4)
        cds_id = f'chr{chr_num}_start{start}_end{end}'
    else:
        cds_id = phy_filename.replace('.phy', '')
        group = None
    logging.info(f"Identified CDS: {cds_id}")

    # Check if output files already exist
    output_csv = os.path.join(output_dir, f'{cds_id}.csv')
    haplotype_output_csv = os.path.join(output_dir, f'{cds_id}_haplotype_stats.csv')

    if os.path.exists(output_csv) and os.path.exists(haplotype_output_csv):
        logging.info(f"Skipping {phy_file} - output files already exist")
        return haplotype_output_csv

    # Parse sequences and validate
    sequences = parse_phy_file(phy_file)
    if not sequences:
        logging.error(f"No valid sequences found in {phy_file}")
        return None
       
    # Get sample names and assign groups
    sample_names = list(sequences.keys())
    logging.info(f"Found {len(sample_names)} samples")

    sample_groups = {}
    for sample in sample_names:
        sample_group = extract_group_from_sample(sample)
        sample_groups[sample] = sample_group if sample_group is not None else group
    logging.info(f"Assigned {len(sample_groups)} samples to groups")

    # Separate sequences by group and only generate within-group pairs
    group0_names = [name for name, grp in sample_groups.items() if grp == 0]
    group1_names = [name for name, grp in sample_groups.items() if grp == 1]
    
    logging.info(f"Found {len(group0_names)} sequences in group 0")
    logging.info(f"Found {len(group1_names)} sequences in group 1")

    # Generate pairs ONLY within same group
    group0_pairs = list(combinations(group0_names, 2))
    group1_pairs = list(combinations(group1_names, 2))
    pairs = group0_pairs + group1_pairs
    
    total_pairs = len(pairs)
    logging.info(f"Generated {len(group0_pairs)} pairs within group 0")
    logging.info(f"Generated {len(group1_pairs)} pairs within group 1")
    logging.info(f"Total pairs to process: {total_pairs}")

    if total_pairs == 0:
        logging.error(f"No valid pairs to process in {phy_file}")
        return None

    # Create temporary directory
    timestamp = int(time.time())
    temp_dir = os.path.join(output_dir, f'temp_{cds_id}_{timestamp}')
    os.makedirs(temp_dir, exist_ok=True)

    # Prepare multiprocessing arguments
    pool_args = [(pair, sequences, sample_groups, cds_id, codeml_path, temp_dir) for pair in pairs]
    num_processes = get_safe_process_count()
    logging.info(f"Processing {total_pairs} pairs using {num_processes} processes")
    
    # Process pairs using multiprocessing
    results = []
    with multiprocessing.Pool(processes=num_processes) as pool:
        completed = 0
        total_steps = max(1, total_pairs // 20)
        for result in pool.imap_unordered(process_pair, pool_args):
            if result is not None:  # Only append valid results
                # Verify groups match before adding result
                if result[2] == result[3]:  # Check Group1 == Group2
                    results.append(result)
                else:
                    logging.warning(f"Discarding result with mismatched groups: {result[0]} (Group {result[2]}) vs {result[1]} (Group {result[3]})")
            completed += 1
            if completed % total_steps == 0 or completed == total_pairs:
                logging.info(f"Progress: {completed}/{total_pairs} pairs ({(completed/total_pairs)*100:.1f}%)")
                logging.info(f"Current runtime: {time.time() - start_time:.1f}s")

    logging.info(f"All pairs processed for {phy_file}")
    
    # Clean up temporary directory
    try:
        shutil.rmtree(temp_dir)
        logging.info(f"Cleaned up temporary directory: {temp_dir}")
    except Exception as e:
        logging.warning(f"Failed to clean up {temp_dir}: {str(e)}")

    # Create and save pairwise results DataFrame
    df = pd.DataFrame(results, columns=['Seq1', 'Seq2', 'Group1', 'Group2', 'dN', 'dS', 'omega', 'CDS'])
    
    # Verify no cross-group comparisons exist in final results
    cross_group = df[df['Group1'] != df['Group2']]
    if not cross_group.empty:
        logging.error("Found cross-group comparisons in results!")
        logging.error(cross_group)
        return None
    
    df.to_csv(output_csv, index=False)
    logging.info(f"Saved pairwise results to: {output_csv}")

    # Calculate per-haplotype statistics (now guaranteed to be within-group only)
    haplotype_stats = []
    for sample in sample_names:
        # Get only comparisons where this sample is involved
        sample_df = df[(df['Seq1'] == sample) | (df['Seq2'] == sample)]
        # Convert omega to numeric, handling non-numeric values
        omega_values = pd.to_numeric(sample_df['omega'], errors='coerce')
        
        # Exclude invalid omega values (-1 and 99)
        omega_values = omega_values[~omega_values.isin([-1, 99])]
        
        # Calculate statistics if there are valid omega values
        if not omega_values.empty:
            mean_omega = omega_values.mean()
            median_omega = omega_values.median()
        else:
            mean_omega = np.nan
            median_omega = np.nan
        
        haplotype_stats.append({
            'Haplotype': sample,
            'Group': sample_groups[sample],
            'CDS': cds_id,
            'Mean_dNdS': mean_omega,
            'Median_dNdS': median_omega,
            'Num_Comparisons': len(omega_values)  # Add count of comparisons
        })
        
        # Log sample statistics, handling NaN values
        mean_str = f"{mean_omega:.4f}" if pd.notna(mean_omega) else "N/A"
        median_str = f"{median_omega:.4f}" if pd.notna(median_omega) else "N/A"
        logging.info(f"Sample {sample} (Group {sample_groups[sample]}): mean dN/dS = {mean_str}, median = {median_str}, comparisons = {len(omega_values)}")

    # Save haplotype statistics
    haplotype_df = pd.DataFrame(haplotype_stats)
    haplotype_df.to_csv(haplotype_output_csv, index=False)
    logging.info(f"Saved haplotype statistics to: {haplotype_output_csv}")

    # Calculate and log group statistics
    group0 = haplotype_df[haplotype_df['Group'] == 0]['Mean_dNdS'].dropna()
    group1 = haplotype_df[haplotype_df['Group'] == 1]['Mean_dNdS'].dropna()

    logging.info(f"\nWithin-Group Statistics for CDS {cds_id}:")
    if not group0.empty:
        logging.info(f"Group 0: n={len(group0)}, Mean={group0.mean():.4f}, Median={group0.median():.4f}, SD={group0.std():.4f}")
    else:
        logging.info("Group 0: No valid Mean_dNdS values.")
    if not group1.empty:
        logging.info(f"Group 1: n={len(group1)}, Mean={group1.mean():.4f}, Median={group1.median():.4f}, SD={group1.std():.4f}")
    else:
        logging.info("Group 1: No valid Mean_dNdS values.")

    total_time = time.time() - start_time
    logging.info(f"Completed processing {phy_file} in {total_time:.1f} seconds")
    
    return haplotype_output_csv

def perform_statistical_tests(haplotype_stats_files, output_dir):
    """
    Perform statistical tests:
    1. Mann-Whitney U test on all mean dN/dS between groups.
    2. Test 1: Unpaired Mann-Whitney U test on sample-wise mean dN/dS between groups.
    3. Test 2: Paired Wilcoxon Signed-Rank test on CDS-wise mean dN/dS between groups.
    
    Excludes dN/dS values of -1 and 99 from all tests.
    """
    # Combine all haplotype stats into a single DataFrame
    haplotype_dfs = []
    for f in haplotype_stats_files:
        try:
            df = pd.read_csv(f)
            haplotype_dfs.append(df)
            logging.info(f"Loaded haplotype stats file: {f}")
        except Exception as e:
            logging.error(f"Error reading {f}: {e}")

    if not haplotype_dfs:
        logging.warning("No haplotype statistics files to process.")
        return

    haplotype_df = pd.concat(haplotype_dfs, ignore_index=True)
    logging.info(f"Combined haplotype data contains {len(haplotype_df)} entries")

    # Exclude dN/dS values of -1 and 99
    haplotype_df_filtered = haplotype_df[~haplotype_df['Mean_dNdS'].isin([-1, 99])].copy()
    logging.info(f"Total haplotype entries after exclusion: {len(haplotype_df_filtered)}")

    # Save filtered DataFrame
    filtered_haplotype_csv = os.path.join(output_dir, 'filtered_haplotype_stats.csv')
    haplotype_df_filtered.to_csv(filtered_haplotype_csv, index=False)
    logging.info(f"Saved filtered haplotype statistics to: {filtered_haplotype_csv}")

    # -------------------
    # 3.1 Existing Mann-Whitney U Test on All Mean dN/dS Between Groups
    # -------------------
    group0_overall = haplotype_df_filtered[haplotype_df_filtered['Group'] == 0]['Mean_dNdS'].dropna()
    group1_overall = haplotype_df_filtered[haplotype_df_filtered['Group'] == 1]['Mean_dNdS'].dropna()

    logging.info("\n=== Existing Mann-Whitney U Test on All Mean dN/dS ===")
    if not group0_overall.empty and not group1_overall.empty:
        stat, p_value = mannwhitneyu(group0_overall, group1_overall, alternative='two-sided')
        logging.info(f"Overall Mann-Whitney U test: Statistic={stat}, p-value={p_value:.6f}")
        if p_value < 0.05:
            logging.info("Result: Significant difference between Group 0 and Group 1.")
        else:
            logging.info("Result: No significant difference between Group 0 and Group 1.")
    else:
        logging.warning("Not enough data for the overall Mann-Whitney U test.")

    # -------------------
    # 3.2 Test 1: Unpaired Mann-Whitney U Test on Sample-wise Mean dN/dS
    # -------------------
    logging.info("\n=== Test 1: Unpaired Mann-Whitney U Test on Sample-wise Mean dN/dS ===")
    if not group0_overall.empty and not group1_overall.empty:
        stat1, p_value1 = mannwhitneyu(group0_overall, group1_overall, alternative='two-sided')
        logging.info(f"Test 1 Mann-Whitney U test: Statistic={stat1}, p-value={p_value1:.6f}")
        if p_value1 < 0.05:
            logging.info("Result: Significant difference between Group 0 and Group 1 (Test 1).")
        else:
            logging.info("Result: No significant difference between Group 0 and Group 1 (Test 1).")
    else:
        logging.warning("Not enough data for Test 1.")

    # -------------------
    # 3.3 Test 2: Paired Wilcoxon Signed-Rank Test on CDS-wise Mean dN/dS
    # -------------------
    logging.info("\n=== Test 2: Paired Wilcoxon Signed-Rank Test on CDS-wise Mean dN/dS ===")

    # Extract per-CDS mean dN/dS for each group
    cds_group_means = haplotype_df_filtered.groupby(['CDS', 'Group'])['Mean_dNdS'].mean().unstack()

    # Drop CDSs that do not have both groups
    cds_paired = cds_group_means.dropna(subset=[0,1])

    if len(cds_paired) < 1:
        logging.warning("No paired CDS data available for Test 2.")
        return

    # Extract paired group0 and group1 mean dN/dS
    group0_cds = cds_paired[0]
    group1_cds = cds_paired[1]

    # Perform Wilcoxon Signed-Rank Test
    try:
        stat2, p_value2 = wilcoxon(group0_cds, group1_cds)
        logging.info(f"Test 2 Wilcoxon Signed-Rank test: Statistic={stat2}, p-value={p_value2:.6f}")
        if p_value2 < 0.05:
            logging.info("Result: Significant difference between Group 0 and Group 1 (Test 2).")
        else:
            logging.info("Result: No significant difference between Group 0 and Group 1 (Test 2).")
    except ValueError as ve:
        logging.error(f"Test 2 Wilcoxon Signed-Rank test could not be performed: {ve}")

def check_existing_results(output_dir):
    """
    Perform preliminary analysis using existing results files before running PAML.
    Groups are determined by the 'Group' column directly from haplotype_stats.csv.
    Performs analyses with different filtering criteria for dN/dS values.
    """
    logging.info("\n=== Performing Preliminary Analysis of Existing Results ===")
    
    # Find all existing haplotype statistics files
    haplotype_files = glob.glob(os.path.join(output_dir, '*_haplotype_stats.csv'))
    if not haplotype_files:
        logging.info("No existing results found for preliminary analysis.")
        return None
        
    logging.info(f"Found {len(haplotype_files)} existing result files")
    
    # Combine all haplotype stats
    haplotype_dfs = []
    for f in haplotype_files:
        try:
            df = pd.read_csv(f)
            haplotype_dfs.append(df)
            logging.info(f"Loaded {f}: {len(df)} entries")
        except Exception as e:
            logging.error(f"Error reading {f}: {e}")
            continue
    
    if not haplotype_dfs:
        logging.warning("No valid data found in existing files")
        return None
        
    # Combine all data
    combined_df = pd.concat(haplotype_dfs, ignore_index=True)
    logging.info(f"Combined data contains {len(combined_df)} entries")
    
    # Log 'Mean_dNdS' statistics
    if 'Mean_dNdS' in combined_df.columns:
        logging.info(f"'Mean_dNdS' Summary:")
        logging.info(combined_df['Mean_dNdS'].describe())
    else:
        logging.error("Combined DataFrame does not contain 'Mean_dNdS' column.")
        return None
    
    # Verify 'Group' column exists and has valid values
    if 'Group' not in combined_df.columns:
        logging.error("Combined DataFrame does not contain 'Group' column.")
        return None
    
    # Ensure 'Group' column is of integer type
    combined_df['Group'] = pd.to_numeric(combined_df['Group'], errors='coerce')
    
    # Check unique groups present
    unique_groups = combined_df['Group'].dropna().unique()
    logging.info(f"Unique groups in combined data: {unique_groups}")
    
    if not set(unique_groups).intersection({0,1}):
        logging.error("No valid groups (0 or 1) found in the 'Group' column.")
        return None
    
    # Create filtered datasets
    df_no_neg1 = combined_df[combined_df['Mean_dNdS'] != -1].copy()
    df_no_99 = combined_df[combined_df['Mean_dNdS'] != 99].copy()
    df_no_both = combined_df[(combined_df['Mean_dNdS'] != -1) & (combined_df['Mean_dNdS'] != 99)].copy()

    datasets = {
        "All data": combined_df,
        "Excluding dN/dS = -1": df_no_neg1,
        "Excluding dN/dS = 99": df_no_99,
        "Excluding both -1 and 99": df_no_both
    }

    # Analyze each dataset
    for dataset_name, df in datasets.items():
        logging.info(f"\n=== Analysis for {dataset_name} ===")
        logging.info(f"Dataset contains {len(df)} entries")

        # Calculate statistics per group
        stats = {}
        for group in [0, 1]:
            group_data = df[df['Group'] == group]['Mean_dNdS'].dropna()
            if not group_data.empty:
                stats[group] = {
                    'n': len(group_data),
                    'mean': group_data.mean(),
                    'median': group_data.median(),
                    'std': group_data.std()
                }
                logging.info(f"\nGroup {group}:")
                logging.info(f"  Sample size: {stats[group]['n']}")
                logging.info(f"  Mean dN/dS: {stats[group]['mean']:.4f}")
                logging.info(f"  Median dN/dS: {stats[group]['median']:.4f}")
                logging.info(f"  Standard deviation: {stats[group]['std']:.4f}")
            else:
                logging.info(f"\nGroup {group}: No valid Mean_dNdS values.")

        # Perform statistical tests if both groups present
        if 0 in stats and 1 in stats:
            group0_data = df[df['Group'] == 0]['Mean_dNdS'].dropna()
            group1_data = df[df['Group'] == 1]['Mean_dNdS'].dropna()
            
            try:
                stat, p_value = mannwhitneyu(group0_data, group1_data, alternative='two-sided')
                logging.info("\nMann-Whitney U test:")
                logging.info(f"  Statistic = {stat}")
                logging.info(f"  p-value = {p_value:.6f}")
                
                # Calculate effect size
                effect_size = abs(stats[0]['mean'] - stats[1]['mean']) / np.sqrt((stats[0]['std']**2 + stats[1]['std']**2) / 2)
                logging.info(f"  Effect size (Cohen's d) = {effect_size:.4f}")
                
                # Interpret results
                if p_value < 0.05:
                    logging.info("  Result: Significant difference between groups")
                else:
                    logging.info("  Result: No significant difference between groups")
                
                # Add additional descriptive statistics
                logging.info("\nAdditional Statistics:")
                logging.info(f"  Group 0 range: {group0_data.min():.4f} to {group0_data.max():.4f}")
                logging.info(f"  Group 1 range: {group1_data.min():.4f} to {group1_data.max():.4f}")
                
                # Calculate and log quartiles
                g0_quartiles = group0_data.quantile([0.25, 0.75])
                g1_quartiles = group1_data.quantile([0.25, 0.75])
                logging.info(f"  Group 0 quartiles (Q1, Q3): {g0_quartiles[0.25]:.4f}, {g0_quartiles[0.75]:.4f}")
                logging.info(f"  Group 1 quartiles (Q1, Q3): {g1_quartiles[0.25]:.4f}, {g1_quartiles[0.75]:.4f}")
                
            except Exception as e:
                logging.error(f"Error performing statistical tests: {str(e)}")

    # Ensure that haplotype_df_filtered is always defined before returning
    haplotype_df_filtered = haplotype_df_filtered if 'haplotype_df_filtered' in locals() else None
    return haplotype_df_filtered

# ----------------------------
# 4. Parsing PHYLIP Files
# ----------------------------

def parse_phy_file(filepath):
    """Parse PHYLIP file with codon-aligned sequences and enforce sample naming convention."""
    logging.info(f"\n=== Starting to parse file: {filepath} ===")
    sequences = {}
    
    with open(filepath, 'r') as file:
        lines = file.readlines()
        if len(lines) < 1:
            logging.error(f"Empty .phy file {filepath}")
            return sequences

        # Attempt to parse the header; if it fails, assume no header
        try:
            num_sequences, seq_length = map(int, lines[0].strip().split())
            logging.info(f"File contains {num_sequences} sequences of length {seq_length}")
            sequence_lines = [line.strip() for line in lines[1:] if line.strip()]
        except ValueError:
            logging.warning(f"Failed parsing header of {filepath}. Assuming no header.")
            sequence_lines = [line.strip() for line in lines if line.strip()]
    
        for line in sequence_lines:
            # Look for the pattern _0 or _1 followed by sequence
            match = re.match(r'^(.+?_[01])\s*(.*)$', line.strip())
            if match:
                full_name = match.group(1)  # e.g., AFR_MSL_HG03486_1
                sequence = match.group(2)    # Sequence part after the name
            else:
                # Fallback to existing parsing if pattern not found
                parts = line.strip().split()
                if len(parts) >= 2:
                    full_name = parts[0]
                    sequence = ''.join(parts[1:])
                else:
                    full_name = line[:10].strip()
                    sequence = line[10:].replace(" ", "")
            
            # Generate checksum
            checksum = generate_checksum(full_name)
            
            # Extract first three characters
            first_three = full_name[:3]
            
            # Extract group suffix (_0 or _1)
            group_suffix_match = re.search(r'_(0|1)$', full_name)
            if group_suffix_match:
                group_suffix = group_suffix_match.group(1)
            else:
                logging.warning(f"Sample name does not end with _0 or _1: {full_name}")
                group_suffix = '0'  # Default to group 0 if not found
            
            # Construct the new sample name: XXX_YYY_S
            new_sample_name = f"{first_three}_{checksum}_{group_suffix}"
            
            # Pad to 10 characters if necessary
            if len(new_sample_name) < 10:
                new_sample_name = new_sample_name.ljust(10)
            else:
                new_sample_name = new_sample_name[:10]
            
            # Validate and clean sequence
            sequence = validate_sequence(sequence)
            if sequence is not None:
                sequences[new_sample_name] = sequence
                logging.info(f"Parsed sequence: {full_name} as {new_sample_name} (length: {len(sequence)})")

    logging.info(f"Successfully parsed {len(sequences)} sequences")
    return sequences

# ----------------------------
# 5. Statistical Analysis
# ----------------------------

def check_existing_results(output_dir):
    """
    Perform preliminary analysis using existing results files before running PAML.
    Groups are determined by the 'Group' column directly from haplotype_stats.csv.
    Performs analyses with different filtering criteria for dN/dS values.
    """
    logging.info("\n=== Performing Preliminary Analysis of Existing Results ===")
    
    # Find all existing haplotype statistics files
    haplotype_files = glob.glob(os.path.join(output_dir, '*_haplotype_stats.csv'))
    if not haplotype_files:
        logging.info("No existing results found for preliminary analysis.")
        return None
        
    logging.info(f"Found {len(haplotype_files)} existing result files")
    
    # Combine all haplotype stats
    haplotype_dfs = []
    for f in haplotype_files:
        try:
            df = pd.read_csv(f)
            haplotype_dfs.append(df)
            logging.info(f"Loaded {f}: {len(df)} entries")
        except Exception as e:
            logging.error(f"Error reading {f}: {e}")
            continue
    
    if not haplotype_dfs:
        logging.warning("No valid data found in existing files")
        return None
        
    # Combine all data
    combined_df = pd.concat(haplotype_dfs, ignore_index=True)
    logging.info(f"Combined data contains {len(combined_df)} entries")
    
    # Log 'Mean_dNdS' statistics
    if 'Mean_dNdS' in combined_df.columns:
        logging.info(f"'Mean_dNdS' Summary:")
        logging.info(combined_df['Mean_dNdS'].describe())
    else:
        logging.error("Combined DataFrame does not contain 'Mean_dNdS' column.")
        return None
    
    # Verify 'Group' column exists and has valid values
    if 'Group' not in combined_df.columns:
        logging.error("Combined DataFrame does not contain 'Group' column.")
        return None
    
    # Ensure 'Group' column is of integer type
    combined_df['Group'] = pd.to_numeric(combined_df['Group'], errors='coerce')
    
    # Check unique groups present
    unique_groups = combined_df['Group'].dropna().unique()
    logging.info(f"Unique groups in combined data: {unique_groups}")
    
    if not set(unique_groups).intersection({0,1}):
        logging.error("No valid groups (0 or 1) found in the 'Group' column.")
        return None
    
    # Create filtered datasets
    df_no_neg1 = combined_df[combined_df['Mean_dNdS'] != -1].copy()
    df_no_99 = combined_df[combined_df['Mean_dNdS'] != 99].copy()
    df_no_both = combined_df[(combined_df['Mean_dNdS'] != -1) & (combined_df['Mean_dNdS'] != 99)].copy()

    datasets = {
        "All data": combined_df,
        "Excluding dN/dS = -1": df_no_neg1,
        "Excluding dN/dS = 99": df_no_99,
        "Excluding both -1 and 99": df_no_both
    }

    # Analyze each dataset
    for dataset_name, df in datasets.items():
        logging.info(f"\n=== Analysis for {dataset_name} ===")
        logging.info(f"Dataset contains {len(df)} entries")

        # Calculate statistics per group
        stats = {}
        for group in [0, 1]:
            group_data = df[df['Group'] == group]['Mean_dNdS'].dropna()
            if not group_data.empty:
                stats[group] = {
                    'n': len(group_data),
                    'mean': group_data.mean(),
                    'median': group_data.median(),
                    'std': group_data.std()
                }
                logging.info(f"\nGroup {group}:")
                logging.info(f"  Sample size: {stats[group]['n']}")
                logging.info(f"  Mean dN/dS: {stats[group]['mean']:.4f}")
                logging.info(f"  Median dN/dS: {stats[group]['median']:.4f}")
                logging.info(f"  Standard deviation: {stats[group]['std']:.4f}")
            else:
                logging.info(f"\nGroup {group}: No valid Mean_dNdS values.")

        # Perform statistical tests if both groups present
        if 0 in stats and 1 in stats:
            group0_data = df[df['Group'] == 0]['Mean_dNdS'].dropna()
            group1_data = df[df['Group'] == 1]['Mean_dNdS'].dropna()
            
            try:
                stat, p_value = mannwhitneyu(group0_data, group1_data, alternative='two-sided')
                logging.info("\nMann-Whitney U test:")
                logging.info(f"  Statistic = {stat}")
                logging.info(f"  p-value = {p_value:.6f}")
                
                # Calculate effect size
                effect_size = abs(stats[0]['mean'] - stats[1]['mean']) / np.sqrt((stats[0]['std']**2 + stats[1]['std']**2) / 2)
                logging.info(f"  Effect size (Cohen's d) = {effect_size:.4f}")
                
                # Interpret results
                if p_value < 0.05:
                    logging.info("  Result: Significant difference between groups")
                else:
                    logging.info("  Result: No significant difference between groups")
                
                # Add additional descriptive statistics
                logging.info("\nAdditional Statistics:")
                logging.info(f"  Group 0 range: {group0_data.min():.4f} to {group0_data.max():.4f}")
                logging.info(f"  Group 1 range: {group1_data.min():.4f} to {group1_data.max():.4f}")
                
                # Calculate and log quartiles
                g0_quartiles = group0_data.quantile([0.25, 0.75])
                g1_quartiles = group1_data.quantile([0.25, 0.75])
                logging.info(f"  Group 0 quartiles (Q1, Q3): {g0_quartiles[0.25]:.4f}, {g0_quartiles[0.75]:.4f}")
                logging.info(f"  Group 1 quartiles (Q1, Q3): {g1_quartiles[0.25]:.4f}, {g1_quartiles[0.75]:.4f}")
                
            except Exception as e:
                logging.error(f"Error performing statistical tests: {str(e)}")

    # Ensure that haplotype_df_filtered is always defined before returning
    haplotype_df_filtered = haplotype_df_filtered if 'haplotype_df_filtered' in locals() else None
    return haplotype_df_filtered

# ----------------------------
# 6. Main Function
# ----------------------------

def main():
    parser = argparse.ArgumentParser(description="Calculate pairwise dN/dS using PAML.")
    parser.add_argument('--phy_dir', type=str, default='.', help='Directory containing .phy files.')
    parser.add_argument('--output_dir', type=str, default='paml_output', help='Directory to store output files.')
    parser.add_argument('--codeml_path', type=str, default='../../../../paml/bin/codeml', help='Path to codeml executable.')
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # Perform preliminary analysis first
    logging.info("\nPerforming preliminary analysis of existing results...")
    prelim_results = check_existing_results(args.output_dir)
    if prelim_results is not None:
        logging.info("\nPreliminary analysis complete. Proceeding with remaining files...")
    else:
        logging.info("\nNo existing results found. Proceeding with full analysis...")
    
    # Get all input files
    phy_files = glob.glob(os.path.join(args.phy_dir, '*.phy'))
    total_files = len(phy_files)
    logging.info(f"Found {total_files} total .phy files")

    # Get the list of files to process
    files_to_process = []
    for phy_file in phy_files:
        phy_filename = os.path.basename(phy_file)
        match = re.match(r'group_(\d+)_chr_(.+)_start_(\d+)_end_(\d+)\.phy', phy_filename)
        if match:
            cds_id = f'chr{match.group(2)}_start{match.group(3)}_end{match.group(4)}'
        else:
            cds_id = phy_filename.replace('.phy', '')
        output_csv = os.path.join(args.output_dir, f'{cds_id}.csv')
        haplotype_output_csv = os.path.join(args.output_dir, f'{cds_id}_haplotype_stats.csv')
        if not os.path.exists(output_csv) or not os.path.exists(haplotype_output_csv):
            files_to_process.append(phy_file)
        else:
            logging.info(f"Skipping {phy_file} - output files already exist")

    if not files_to_process:
        logging.info("All files already processed. Exiting.")
        return

    # Prepare arguments for processing each file
    total_files = len(files_to_process)
    work_args = []
    for idx, phy_file in enumerate(files_to_process, 1):
        work_args.append((phy_file, args.output_dir, args.codeml_path, total_files, idx))

    haplotype_stats_files = []
    for args_tuple in work_args:
        result = process_phy_file(args_tuple)
        if result:
            haplotype_stats_files.append(result)

    # Perform final statistical tests
    if haplotype_stats_files:
        perform_statistical_tests(haplotype_stats_files, args.output_dir)
    else:
        logging.warning("No haplotype statistics files generated.")

if __name__ == '__main__':
    main()
