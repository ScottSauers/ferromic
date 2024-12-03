import pandas as pd
import numpy as np
from collections import defaultdict
import seaborn as sns
import matplotlib.pyplot as plt
from multiprocessing import cpu_count
from concurrent.futures import ProcessPoolExecutor
from tqdm.auto import tqdm
import warnings
import os
import json
from datetime import datetime
from pathlib import Path
import pickle
from scipy import stats

# Suppress warnings
warnings.filterwarnings('ignore')

# Constants
CACHE_DIR = Path("cache")
RESULTS_DIR = Path("results")
PLOTS_DIR = Path("plots")
N_PERMUTATIONS = 10000  # Number of permutations for the permutation test

# Create necessary directories
for directory in [CACHE_DIR, RESULTS_DIR, PLOTS_DIR]:
    directory.mkdir(exist_ok=True)

def get_cache_path(cds):
    """Generate cache file path for a CDS."""
    return CACHE_DIR / f"{cds.replace('/', '_')}.pkl"

def load_cached_result(cds):
    """Load cached result for a CDS if it exists."""
    cache_path = get_cache_path(cds)
    if cache_path.exists():
        try:
            with open(cache_path, 'rb') as f:
                cached_result = pickle.load(f)
            return cached_result
        except:
            return None
    return None

def save_cached_result(cds, result):
    """Save result for a CDS to cache."""
    cache_path = get_cache_path(cds)
    with open(cache_path, 'wb') as f:
        pickle.dump(result, f)

def read_and_preprocess_data(file_path):
    """Read and preprocess the CSV file."""
    print("Reading data...")
    df = pd.read_csv(file_path)

    # Filtering valid omega values
    df = df[
        (df['omega'] != -1) &
        (df['omega'] != 99)
    ].dropna(subset=['omega'])

    print(f"Total valid comparisons: {len(df):,}")
    print(f"Unique CDSs found: {df['CDS'].nunique():,}")
    return df

def get_pairwise_value(seq1, seq2, pairwise_dict):
    """Get omega value for a pair of sequences."""
    key = (seq1, seq2) if (seq1, seq2) in pairwise_dict else (seq2, seq1)
    return pairwise_dict.get(key)

def create_matrices(sequences_0, sequences_1, pairwise_dict):
    """Create matrices for two groups based on sequence assignments."""
    if len(sequences_0) == 0 or len(sequences_1) == 0:
        return None, None

    n0, n1 = len(sequences_0), len(sequences_1)
    matrix_0 = np.full((n0, n0), np.nan)
    matrix_1 = np.full((n1, n1), np.nan)

    # Fill matrix 0
    for i in range(n0):
        for j in range(i + 1, n0):
            val = get_pairwise_value(sequences_0[i], sequences_0[j], pairwise_dict)
            if val is not None:
                matrix_0[i, j] = matrix_0[j, i] = val

    # Fill matrix 1
    for i in range(n1):
        for j in range(i + 1, n1):
            val = get_pairwise_value(sequences_1[i], sequences_1[j], pairwise_dict)
            if val is not None:
                matrix_1[i, j] = matrix_1[j, i] = val

    return matrix_0, matrix_1

def analysis_worker(args):
    """GEE analysis worker function for a single CDS."""
    import statsmodels.api as sm

    all_sequences, n0, pairwise_dict, sequences_0, sequences_1 = args

    # Prepare data for GEE
    data = []
    for (seq1, seq2), omega in pairwise_dict.items():
        # Determine group of the pair
        if seq1 in sequences_0 and seq2 in sequences_0:
            group = 0
        elif seq1 in sequences_1 and seq2 in sequences_1:
            group = 1
        else:
            continue  # Skip pairs not within the same group

        data.append({
            'omega_value': omega,
            'group': group,
            'seq1': seq1,
            'seq2': seq2
        })

    df = pd.DataFrame(data)

    if df.empty:
        return {
            'observed_effect_size': np.nan,
            'p_value': np.nan,
            'n0': n0,
            'n1': len(all_sequences) - n0,
            'num_comp_group_0': 0,
            'num_comp_group_1': 0,
            'std_err': np.nan
        }

    # Check if 'group' has at least two levels
    if df['group'].nunique() < 2:
        return {
            'observed_effect_size': np.nan,
            'p_value': np.nan,
            'n0': n0,
            'n1': len(all_sequences) - n0,
            'num_comp_group_0': (df['group'] == 0).sum(),
            'num_comp_group_1': (df['group'] == 1).sum(),
            'std_err': np.nan
        }

    # Check if 'omega_value' has variation
    if df['omega_value'].nunique() < 2:
        return {
            'observed_effect_size': np.nan,
            'p_value': np.nan,
            'n0': n0,
            'n1': len(all_sequences) - n0,
            'num_comp_group_0': (df['group'] == 0).sum(),
            'num_comp_group_1': (df['group'] == 1).sum(),
            'std_err': np.nan
        }

    # Add constant term for intercept
    df['intercept'] = 1

    # Define dependent and independent variables
    endog = df['omega_value']
    exog = df[['intercept', 'group']]

    # Since dependencies are assumed minimal, use Independence correlation structure
    family = sm.families.Gaussian()
    ind = sm.cov_struct.Independence()

    # Fit GEE model with exception handling
    try:
        model = sm.GEE(endog, exog, groups=np.arange(len(df)), family=family, cov_struct=ind)
        result = model.fit()
    except (ValueError, np.linalg.LinAlgError):
        # Return NaNs if the model fails to fit
        return {
            'observed_effect_size': np.nan,
            'p_value': np.nan,
            'n0': n0,
            'n1': len(all_sequences) - n0,
            'std_err': np.nan,
            'num_comp_group_0': (df['group'] == 0).sum(),
            'num_comp_group_1': (df['group'] == 1).sum()
        }

    # Extract effect size and p-value
    effect_size = result.params.get('group', np.nan)
    p_value = result.pvalues.get('group', np.nan)
    std_err = result.bse.get('group', np.nan)

    return {
        'observed_effect_size': effect_size,
        'p_value': p_value,
        'n0': n0,
        'n1': len(all_sequences) - n0,
        'std_err': std_err,
        'num_comp_group_0': (df['group'] == 0).sum(),
        'num_comp_group_1': (df['group'] == 1).sum(),
    }


def compute_cliffs_delta(x, y):
    """Compute Cliff's Delta effect size."""
    n_x = len(x)
    n_y = len(y)
    n_pairs = n_x * n_y

    # Efficient computation using broadcasting
    x = np.array(x)
    y = np.array(y)

    difference_matrix = x[:, np.newaxis] - y
    num_greater = np.sum(difference_matrix > 0)
    num_less = np.sum(difference_matrix < 0)

    cliffs_delta = (num_greater - num_less) / n_pairs

    return cliffs_delta

def create_visualization(matrix_0, matrix_1, cds, result):
    """Create visualizations for a CDS."""
    if matrix_0 is None or matrix_1 is None:
        return

    # Create figure without any specific style
    fig = plt.figure(figsize=(20, 10))

    # Main title
    plt.suptitle(f'Pairwise Comparison Analysis: {cds}',
                fontsize=16, fontweight='bold', y=1.02)

    gs = plt.GridSpec(2, 3, figure=fig)

    # Heatmaps
    ax1 = fig.add_subplot(gs[0, 0])
    ax2 = fig.add_subplot(gs[0, 1])

    # Custom diverging colormap
    cmap = sns.diverging_palette(220, 20, as_cmap=True)

    # Plot heatmaps
    sns.heatmap(matrix_0, cmap=cmap, center=1, ax=ax1,
                square=True, cbar_kws={'label': 'Omega Value'})
    sns.heatmap(matrix_1, cmap=cmap, center=1, ax=ax2,
                square=True, cbar_kws={'label': 'Omega Value'})

    ax1.set_title(f'Group 0 Matrix (n={result["n0"]})', fontsize=12, pad=10)
    ax2.set_title(f'Group 1 Matrix (n={result["n1"]})', fontsize=12, pad=10)

    # Distribution comparison
    ax3 = fig.add_subplot(gs[0, 2])
    values_0 = matrix_0[np.triu_indices_from(matrix_0, k=1)]
    values_1 = matrix_1[np.triu_indices_from(matrix_1, k=1)]

    sns.kdeplot(data=values_0[~np.isnan(values_0)], ax=ax3, label='Group 0',
                fill=True, alpha=0.5)
    sns.kdeplot(data=values_1[~np.isnan(values_1)], ax=ax3, label='Group 1',
                fill=True, alpha=0.5)
    ax3.set_title('Distribution of Omega Values', fontsize=12)
    ax3.legend()

    # Results table
    ax4 = fig.add_subplot(gs[1, :])
    ax4.axis('off')

    table_data = [
        ['Metric', 'Value'],
        ['Observed Effect Size (Cliff\'s Delta)', f"{result['observed_effect_size']:.4f}"],
        ['P-value', f"{result['p_value']:.4f}"]
    ]

    table = ax4.table(cellText=table_data, loc='center', cellLoc='center',
                     colWidths=[0.3, 0.2])
    table.auto_set_font_size(False)
    table.set_fontsize(12)
    table.scale(1.5, 2)

    # Style the table
    for (row, col), cell in table.get_celld().items():
        if row == 0:
            cell.set_text_props(weight='bold')
            cell.set_facecolor('#E6E6E6')
        if col == 0:
            cell.set_text_props(weight='bold')

    plt.tight_layout()
    plt.savefig(PLOTS_DIR / f'analysis_{cds.replace("/", "_")}.png',
                dpi=300, bbox_inches='tight')
    plt.close()

def analyze_cds_parallel(args):
    """Analyze a single CDS using GEE."""
    df_cds, cds = args

    # Check cache first
    cached_result = load_cached_result(cds)
    if cached_result is not None:
        return cds, cached_result

    # Create pairwise dictionary
    pairwise_dict = {(row['Seq1'], row['Seq2']): row['omega']
                     for _, row in df_cds.iterrows()}

    # Collect all unique sequences from both 'Seq1' and 'Seq2' columns
    all_seqs = pd.concat([df_cds['Seq1'], df_cds['Seq2']]).unique()
    sequences_0 = np.array([seq for seq in all_seqs if not seq.endswith('1')])
    sequences_1 = np.array([seq for seq in all_seqs if seq.endswith('1')])
    all_sequences = np.concatenate([sequences_0, sequences_1])

    n0 = len(sequences_0)
    n1 = len(sequences_1)

    # Set minimum required sequences per group
    min_sequences_per_group = 5

    if n0 < min_sequences_per_group or n1 < min_sequences_per_group:
        # Not enough sequences in one of the groups; return NaNs
        result = {
            'observed_effect_size': np.nan,
            'p_value': np.nan,
            'n0': n0,
            'n1': n1,
            'num_comp_group_0': 0,
            'num_comp_group_1': 0,
            'std_err': np.nan
        }
    else:
        # Generate matrices for visualization
        matrix_0, matrix_1 = create_matrices(sequences_0, sequences_1, pairwise_dict)

        # Call the GEE analysis worker
        result = analysis_worker((
            all_sequences, n0, pairwise_dict,
            sequences_0, sequences_1
        ))

        # Include matrices in the result dictionary
        result['matrix_0'] = matrix_0
        result['matrix_1'] = matrix_1

    # Pairwise comparisons for cluster analysis
    result['pairwise_comparisons'] = set(pairwise_dict.keys())

    # Cache the result
    save_cached_result(cds, result)
    return cds, result

def parse_cds_coordinates(cds_name):
    """Extract chromosome and coordinates from CDS name."""
    try:
        # Try different possible formats
        if '/' in cds_name:  # If it's a path, take the last part
            cds_name = cds_name.split('/')[-1]

        if '_' in cds_name:  # Expected format
            parts = cds_name.split('_')
            if len(parts) == 3 and parts[1].startswith('start') and parts[2].startswith('end'):
                chrom = parts[0]
                start = int(parts[1].replace('start', ''))
                end = int(parts[2].replace('end', ''))
                return chrom, start, end
        elif ':' in cds_name:
            chrom, coords = cds_name.split(':')
            start, end = map(int, coords.replace('-', '..').split('..'))
            return chrom, start, end

        # If parsing fails
        print(f"Failed to parse: {cds_name}")
        return None, None, None
    except Exception as e:
        print(f"Error parsing {cds_name}: {str(e)}")
        return None, None, None

def build_overlap_clusters(results_df):
    """Build clusters of overlapping CDS regions."""
    print(f"\nAnalyzing {len(results_df)} CDS entries")

    # Initialize clusters
    clusters = {}
    cluster_id = 0
    cds_to_cluster = {}

    # Sort CDSs by chromosome and start position
    cds_coords = []
    for cds in results_df['CDS']:
        chrom, start, end = parse_cds_coordinates(cds)
        if None not in (chrom, start, end):
            cds_coords.append((chrom, start, end, cds))

    print(f"\nSuccessfully parsed {len(cds_coords)} CDS coordinates")
    if len(cds_coords) == 0:
        print("No CDS coordinates could be parsed! Check CDS name format.")
        # Print a few example CDS names
        print("\nExample CDS names:")
        for cds in results_df['CDS'].head():
            print(cds)

    # Sort by chromosome and start position
    cds_coords.sort()

    # Build clusters
    active_regions = []  # (chrom, end, cluster_id)

    for chrom, start, end, cds in cds_coords:
        # Remove finished active regions
        active_regions = [(c, e, cid) for c, e, cid in active_regions
                          if c != chrom or e >= start]

        # Find overlapping clusters
        overlapping_clusters = set(cid for c, e, cid in active_regions
                                   if c == chrom and e >= start)

        if not overlapping_clusters:
            # Create new cluster
            clusters[cluster_id] = {cds}
            cds_to_cluster[cds] = cluster_id
            active_regions.append((chrom, end, cluster_id))
            cluster_id += 1
        else:
            # Merge overlapping clusters
            target_cluster = min(overlapping_clusters)
            clusters[target_cluster].add(cds)
            cds_to_cluster[cds] = target_cluster

            # Merge other overlapping clusters
            for cid in overlapping_clusters:
                if cid != target_cluster:
                    clusters[target_cluster].update(clusters[cid])
                    del clusters[cid]

            # Update active regions
            active_regions = [(c, e, target_cluster) if cid in overlapping_clusters
                              else (c, e, cid)
                              for c, e, cid in active_regions]
            active_regions.append((chrom, end, target_cluster))

    return clusters

def combine_cluster_evidence(cluster_cdss, results_df, results):
    """Combine statistics for a cluster of overlapping CDSs."""
    cluster_data = results_df[results_df['CDS'].isin(cluster_cdss)]

    # Get weights based on CDS length
    weights = {}
    total_length = 0
    for cds in cluster_cdss:
        _, start, end = parse_cds_coordinates(cds)
        if None not in (start, end):
            length = end - start
            weights[cds] = length
            total_length += length

    # Normalize weights
    for cds in weights:
        weights[cds] /= total_length

    # Initialize statistics
    weighted_effect_size = 0
    valid_cdss = 0
    valid_indices = []

    # Initialize a set to collect unique pairwise comparisons for the cluster
    cluster_pairs = set()

    for idx, row in cluster_data.iterrows():
        cds = row['CDS']
        effect_size = row['observed_effect_size']

        if np.isnan(effect_size):
            continue  # Skip invalid entries

        weight = weights.get(cds, 1 / len(cluster_cdss))
        weighted_effect_size += effect_size * weight

        # Accumulate unique pairwise comparisons from the results dictionary
        cds_pairs = results[cds]['pairwise_comparisons']
        cluster_pairs.update(cds_pairs)

        valid_cdss += 1
        valid_indices.append(idx)

    # After the loop, set total_comparisons to the number of unique pairs
    total_comparisons = len(cluster_pairs)

    # Combine p-values if we have valid data
    if valid_cdss > 0:
        # Use Fisher's method within cluster
        valid_pvals = cluster_data.loc[valid_indices]['p_value']

        # Filter out invalid p-values
        valid_pvals = valid_pvals[~np.isnan(valid_pvals)]
        valid_pvals = valid_pvals[~np.isinf(valid_pvals)]

        # Check for zero p-values and print warning
        if (valid_pvals == 0).any():
            print(f"Warning: Zero p-value detected in cluster for CDSs {cluster_cdss}.")
            # Optionally, you might want to investigate why the p-value is zero
            # For example, inspect the corresponding CDSs or data

        if len(valid_pvals) > 0:
            # Proceed with Fisher's method
            fisher_stat = -2 * np.sum(np.log(valid_pvals))
            combined_p = stats.chi2.sf(fisher_stat, df=2 * len(valid_pvals))
            # Handle numerical underflow if combined_p is zero
            if combined_p == 0:
                combined_p = np.nextafter(0, 1)  # Smallest positive float
                print(f"Warning: Combined p-value underflow to zero for cluster with CDSs {cluster_cdss}. Set to {combined_p}.")
        else:
            combined_p = np.nan
    else:
        combined_p = np.nan
        weighted_effect_size = np.nan
        total_comparisons = 0

    return {
        'combined_pvalue': combined_p,
        'weighted_effect_size': weighted_effect_size,
        'n_comparisons': total_comparisons,
        'n_valid_cds': valid_cdss,
        'cluster_pairs': cluster_pairs
    }

def compute_overall_significance(cluster_results):
    """Compute overall significance from independent clusters using Fisher's and Stouffer's methods."""
    import numpy as np
    from scipy import stats

    # Initialize default return values
    overall_pvalue_fisher = np.nan
    overall_pvalue_stouffer = np.nan
    overall_effect = np.nan
    n_valid_clusters = 0
    total_comparisons = 0
    cluster_pvals = None

    # Filter out clusters with valid combined_pvalue and weighted_effect_size
    valid_clusters = [
        c for c in cluster_results.values()
        if not np.isnan(c['combined_pvalue']) and not np.isnan(c['weighted_effect_size'])
    ]

    if valid_clusters:
        # Collect cluster p-values
        cluster_pvals = [c['combined_pvalue'] for c in valid_clusters]

        # Convert cluster_pvals to a numpy array for manipulation
        cluster_pvals = np.array(cluster_pvals)

        # Replace zeros or extremely small p-values with a minimum positive value
        min_pvalue = np.nextafter(0, 1)  # Smallest positive float
        if (cluster_pvals == 0).any():
            print("Warning: Zero p-value detected among cluster combined p-values.")
            zero_p_clusters = [i for i, p in enumerate(cluster_pvals) if p == 0]
            print(f"Clusters with zero p-values: {zero_p_clusters}")
        cluster_pvals[cluster_pvals < min_pvalue] = min_pvalue

        # Fisher's method
        fisher_stat = -2 * np.sum(np.log(cluster_pvals))
        overall_pvalue_fisher = stats.chi2.sf(fisher_stat, df=2 * len(cluster_pvals))
        if overall_pvalue_fisher == 0:
            overall_pvalue_fisher = min_pvalue
            print(f"Warning: Overall p-value underflow to zero in Fisher's method. Set to {overall_pvalue_fisher}.")

        # Stouffer's Z method
        from scipy.stats import norm
        cluster_zscores = norm.isf(cluster_pvals)  # Convert p-values to Z-scores

        # Use the number of comparisons as weights
        weights = np.array([c['n_comparisons'] for c in valid_clusters], dtype=float)

        # Check for zero weights to avoid division by zero
        if np.all(weights == 0):
            weights = None
            print("Warning: All weights are zero. Proceeding with equal weights in Stouffer's method.")
        else:
            # Normalize weights
            weights_sum = weights.sum()
            if weights_sum == 0:
                normalized_weights = np.ones_like(weights) / len(weights)
            else:
                normalized_weights = weights / weights_sum

        # Compute combined Z-score
        if weights is not None:
            combined_z = np.sum(cluster_zscores * normalized_weights)
        else:
            combined_z = np.mean(cluster_zscores)

        # Compute overall p-value using Stouffer's method
        overall_pvalue_stouffer = norm.sf(combined_z)
        if overall_pvalue_stouffer == 0:
            overall_pvalue_stouffer = min_pvalue
            print(f"Warning: Overall p-value underflow to zero in Stouffer's method. Set to {overall_pvalue_stouffer}.")

        # Compute weighted effect size with normalized weights
        effect_sizes = np.array([c['weighted_effect_size'] for c in valid_clusters])
        if weights is not None:
            overall_effect = np.average(effect_sizes, weights=normalized_weights)
        else:
            overall_effect = np.mean(effect_sizes)

        # Collect all unique pairwise comparisons across valid clusters
        all_unique_pairs = set()
        for c in valid_clusters:
            all_unique_pairs.update(c['cluster_pairs'])
        total_comparisons = len(all_unique_pairs)
        n_valid_clusters = len(valid_clusters)

        # Print both p-values to the terminal
        print("\nOverall p-values from combining methods:")
        print(f"Fisher's method p-value: {overall_pvalue_fisher:.4e}")
        print(f"Stouffer's Z method p-value: {overall_pvalue_stouffer:.4e}")

        # Decide which overall p-value to return; let's choose Fisher's method
        overall_pvalue = overall_pvalue_fisher

    else:
        print("No valid clusters available for significance computation.")
        overall_pvalue = np.nan  # overall_pvalue is defined even if no valid clusters

    return {
        'overall_pvalue': overall_pvalue,
        'overall_pvalue_fisher': overall_pvalue_fisher,
        'overall_pvalue_stouffer': overall_pvalue_stouffer,
        'overall_effect': overall_effect,
        'n_valid_clusters': n_valid_clusters,
        'total_comparisons': total_comparisons
    }


def main():
    start_time = datetime.now()
    print(f"Analysis started at {start_time}")

    # Read data
    df = read_and_preprocess_data('all_pairwise_results.csv')

    # Prepare arguments for parallel processing
    cds_list = df['CDS'].unique()
    cds_args = [(df[df['CDS'] == cds], cds) for cds in cds_list]

    # Process CDSs in parallel
    results = {}
    with ProcessPoolExecutor(max_workers=cpu_count()) as executor:
        for cds, result in tqdm(
            executor.map(analyze_cds_parallel, cds_args),
            total=len(cds_args),
            desc="Processing CDSs"
        ):
            results[cds] = result

    # Convert results to DataFrame
    results_df = pd.DataFrame([
        {
            'CDS': cds,
            **{k: v for k, v in result.items()
               if k not in ['matrix_0', 'matrix_1', 'pairwise_comparisons']}
        }
        for cds, result in results.items()
    ])

    # Save final results
    results_df.to_csv(RESULTS_DIR / 'final_results.csv', index=False)

    # Overall analysis
    print("\nComputing overall significance...")
    clusters = build_overlap_clusters(results_df)
    cluster_stats = {}
    for cluster_id, cluster_cdss in clusters.items():
        cluster_stats[cluster_id] = combine_cluster_evidence(cluster_cdss, results_df, results)


    # Compute overall significance
    overall_results = compute_overall_significance(cluster_stats)

    # Convert numpy values to native Python types for JSON serialization
    json_safe_results = {
        'overall_pvalue_fisher': float(overall_results['overall_pvalue_fisher']) if not np.isnan(overall_results['overall_pvalue_fisher']) else None,
        'overall_pvalue_stouffer': float(overall_results['overall_pvalue_stouffer']) if not np.isnan(overall_results['overall_pvalue_stouffer']) else None,
        'overall_effect': float(overall_results['overall_effect']) if not np.isnan(overall_results['overall_effect']) else None,
        'n_valid_clusters': int(overall_results['n_valid_clusters']) if not np.isnan(overall_results['n_valid_clusters']) else None,
        'total_comparisons': int(overall_results['total_comparisons']) if not np.isnan(overall_results['total_comparisons']) else None
    }

    # Save overall results
    with open(RESULTS_DIR / 'overall_results.json', 'w') as f:
        json.dump(json_safe_results, f, indent=2)

    # Print overall results
    print("\nOverall Analysis Results:")
    print(f"Number of independent clusters: {overall_results['n_valid_clusters']}")
    print(f"Total unique CDS pairs: {overall_results['total_comparisons']:,}")
    print(f"Overall p-value: {overall_results['overall_pvalue']:.4e}")
    print(f"Overall effect size (Cliff's Delta): {overall_results['overall_effect']:.4f}")

    # Sort results by p-value
    significant_results = results_df.sort_values('p_value')

    # Create visualizations for top significant results
    for _, row in significant_results.head().iterrows():
        cds = row['CDS']
        result = results[cds]
        create_visualization(
            result['matrix_0'],
            result['matrix_1'],
            cds,
            result
        )

    # Print summary statistics
    valid_results = results_df[~results_df['p_value'].isna()]
    print("\nPer-CDS Analysis Summary:")
    print(f"Total CDSs analyzed: {len(results_df):,}")
    print(f"Valid analyses: {len(valid_results):,}")
    print(f"Significant CDSs (p < 0.05): {(valid_results['p_value'] < 0.05).sum():,}")

    end_time = datetime.now()
    print(f"\nAnalysis completed at {end_time}")
    print(f"Total runtime: {end_time - start_time}")

if __name__ == "__main__":
    main()
