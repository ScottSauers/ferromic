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
from scipy.stats import combine_pvalues
from matplotlib.colors import ListedColormap, BoundaryNorm, LinearSegmentedColormap
from matplotlib.colorbar import ColorbarBase
import matplotlib.patches as mpatches
import requests
from urllib.parse import urlencode


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
    
    # Do not filter out omega values; include all omega values
    df = df.dropna(subset=['omega'])
    
    # Assign sequences to groups based on whether they end with '1' before making IDs unique
    def assign_group(seq_id):
        return 1 if str(seq_id).endswith('1') else 0
    
    # Collect all unique sequence IDs from both 'Seq1' and 'Seq2' columns
    unique_sequence_ids = pd.concat([df['Seq1'], df['Seq2']]).dropna().unique()
    
    # Create a mapping from original sequence IDs to unique IDs
    seq_id_mapping = {}
    sequence_group = {}
    seq_id_counts = defaultdict(int)
    
    for seq_id in unique_sequence_ids:
        # Increment count for this sequence ID
        seq_id_counts[seq_id] += 1
        # Create a unique ID by appending the count
        unique_id = f"{seq_id}_{seq_id_counts[seq_id]}"
        seq_id_mapping[seq_id, seq_id_counts[seq_id]] = unique_id
        # Assign group to the unique ID
        sequence_group[unique_id] = assign_group(seq_id)
    
    # Now, map the original Seq1 and Seq2 IDs to unique IDs
    df['Seq1_original'] = df['Seq1']
    df['Seq2_original'] = df['Seq2']
    
    # We need to map based on the sequence occurrence, so create helper columns
    df['Seq1_occurrence'] = df.groupby('Seq1').cumcount() + 1
    df['Seq2_occurrence'] = df.groupby('Seq2').cumcount() + 1
    
    df['Seq1'] = df.apply(lambda x: seq_id_mapping.get((x['Seq1'], x['Seq1_occurrence'])), axis=1)
    df['Seq2'] = df.apply(lambda x: seq_id_mapping.get((x['Seq2'], x['Seq2_occurrence'])), axis=1)
    
    # Now, create lists of sequences in each group using the unique IDs
    group_0_seqs = [seq_id for seq_id, group in sequence_group.items() if group == 0]
    group_1_seqs = [seq_id for seq_id, group in sequence_group.items() if group == 1]

    # Print the counts for each group
    print(f"Total unique sequence IDs in Group 0: {len(group_0_seqs):,}")
    print(f"Total unique sequence IDs in Group 1: {len(group_1_seqs):,}")

    print(f"Total valid comparisons: {len(df):,}")
    print(f"Unique CDSs found: {df['CDS'].nunique():,}")
    return df




def get_pairwise_value(seq1, seq2, pairwise_dict):
    """Get omega value for a pair of sequences."""
    seq1 = str(seq1)
    seq2 = str(seq2)
    key = (seq1, seq2) if (seq1, seq2) in pairwise_dict else (seq2, seq1)
    val = pairwise_dict.get(key)
    if val is None:
        print(f"\n=== DEBUG: Failed pairwise lookup ===")
        print(f"Tried keys: {(seq1, seq2)}, {(seq2, seq1)}")
        print(f"Key type attempted: {type((seq1, seq2))}")
        print(f"Sample dict key type: {type(list(pairwise_dict.keys())[0])}")
    return val

def create_matrices(sequences_0, sequences_1, pairwise_dict):
    """Create matrices for two groups based on sequence assignments."""
    print("\n=== DEBUG: create_matrices ===")
    print(f"Number of sequences: Group 0={len(sequences_0)}, Group 1={len(sequences_1)}")
    print("Sample sequences_0:", sequences_0[:3])
    print("Sample sequences_1:", sequences_1[:3])
    print("Sample pairwise_dict keys:", list(pairwise_dict.keys())[:3])
    print("Sample pairwise_dict values:", list(pairwise_dict.values())[:3])

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
    """Mixed effects analysis for a single CDS with crossed random effects."""
    import pandas as pd
    import statsmodels.api as sm
    from statsmodels.regression.mixed_linear_model import MixedLM
    import numpy as np

    all_sequences, n0, pairwise_dict, sequences_0, sequences_1 = args

    # Prepare data
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

    # Initialize variables
    effect_size = np.nan
    p_value = np.nan
    std_err = np.nan

    # Check if DataFrame has sufficient data
    if df.empty or df['group'].nunique() < 2 or df['omega_value'].nunique() < 2:
        return {
            'observed_effect_size': effect_size,
            'p_value': p_value,
            'n0': n0,
            'n1': len(all_sequences) - n0,
            'num_comp_group_0': (df['group'] == 0).sum() if not df.empty else 0,
            'num_comp_group_1': (df['group'] == 1).sum() if not df.empty else 0,
            'std_err': std_err
        }

    # Convert sequences to categorical codes
    df['seq1_code'] = pd.Categorical(df['seq1']).codes
    df['seq2_code'] = pd.Categorical(df['seq2']).codes

    # Print diagnostic info
    print(f"\nAnalyzing CDS with:")
    print(f"Data shape: {df.shape}")
    print(f"Unique seq1_codes: {len(df['seq1_code'].unique())}")
    print(f"Unique seq2_codes: {len(df['seq2_code'].unique())}")
    print(f"Group counts:\n{df['group'].value_counts()}")

    try:
        # Prepare data for MixedLM
        # Introduce a dummy 'groups' variable since we'll specify random effects via 'vc_formula'
        df['groups'] = 1  # All data belongs to the same group for variance components

        # Define variance components for crossed random effects
        vc = {
            'seq1': '0 + C(seq1_code)',
            'seq2': '0 + C(seq2_code)'
        }

        # Fit the mixed model using MixedLM.from_formula
        model = MixedLM.from_formula(
            'omega_value ~ group',
            groups='groups',
            vc_formula=vc,
            re_formula='0',  # No random intercept for 'groups' since it's a dummy
            data=df
        )
        result = model.fit(reml=True)  # Or use ML estimation where reml=False?

        # Extract results
        effect_size = result.fe_params['group']
        p_value = result.pvalues['group']
        std_err = result.bse['group']

        print(f"Successfully fit model:")
        print(f"Effect size: {effect_size:.4f}")
        print(f"P-value: {p_value:.4e}")
        print(f"Std error: {std_err:.4f}")

    except Exception as e:
        print(f"Model fitting failed with error: {str(e)}")
        # Variables remain as initialized (np.nan)

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




def get_gene_info(gene_symbol):
    """Get human-readable gene info from MyGene.info using gene symbol"""
    try:
        # Query by symbol to get gene info
        url = f"http://mygene.info/v3/query?q=symbol:{gene_symbol}&species=human&fields=name"
        print(f"\nQuerying: {url}")  # Debug print
        response = requests.get(url, timeout=10)
        print(f"Response status: {response.status_code}")  # Debug print
        if response.ok:
            data = response.json()
            print(f"Raw response: {response.text}")  # Debug print
            if data.get('hits') and len(data['hits']) > 0:
                return data['hits'][0].get('name', 'Unknown')
    except Exception as e:
        print(f"Error fetching gene info: {str(e)}")
    return 'Unknown'




def get_gene_annotation(cds, cache_file='gene_name_cache.json'):
    """
    Get gene annotation for a CDS with caching and detailed error reporting
    Returns (gene_symbol, gene_name, error_log) where error_log contains any warnings/errors
    Uses UCSC API to look up genes that overlap with the given coordinates
    """
    error_log = []
    
    # Load cache if it exists
    cache = {}
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'r') as f:
                cache = json.load(f)
    except json.JSONDecodeError as e:
        error_log.append(f"WARNING: Cache file corrupt or invalid JSON: {str(e)}")
    except Exception as e:
        error_log.append(f"WARNING: Failed to load cache file: {str(e)}")

    # Check cache first
    if cds in cache:
        error_log.append(f"INFO: Found entry in cache for {cds}")
        return cache[cds]['symbol'], cache[cds]['name'], error_log

    def parse_coords(coord_str):
        """Parse coordinate string into chr, start, end"""
        if not coord_str:
            return None, "ERROR: Empty coordinate string provided"
            
        try:
            parts = coord_str.split('_start')
            if len(parts) != 2:
                return None, "ERROR: Invalid coordinate format - missing '_start'"
                
            chr = parts[0]
            start_end = parts[1].split('_end')
            if len(start_end) != 2:
                return None, "ERROR: Invalid coordinate format - missing '_end'"
                
            start = int(start_end[0])
            end = int(start_end[1])
            
            if start > end:
                return None, f"ERROR: Invalid coordinates - start ({start}) greater than end ({end})"
                
            return {'chr': chr, 'start': start, 'end': end}, None
            
        except ValueError as e:
            return None, f"ERROR: Failed to parse coordinates - invalid numbers: {str(e)}"
        except Exception as e:
            return None, f"ERROR: Failed to parse coordinates: {str(e)}"

    def query_ucsc(chr, start, end):
        """Query UCSC API for genes at location"""
        base_url = "https://api.genome.ucsc.edu/getData/track"
        params = {
            'genome': 'hg38',
            'track': 'knownGene',
            'chrom': chr,
            'start': start,
            'end': end
        }
        
        try:
            url = f"{base_url}?{urlencode(params)}"
            print(f"\nQuerying UCSC API URL: {url}")  # Debug print
            response = requests.get(url, timeout=10)
            
            print(f"\nResponse status: {response.status_code}")  # Debug print
            #print(f"\nRaw API Response:\n{response.text}")  # Debug print
            
            if not response.ok:
                return None, f"ERROR: API request failed with status {response.status_code}: {response.text}"
                
            data = response.json()
            
            # Print the structure of the response
            print("\nResponse keys:", data.keys())
            
            if not data:
                return None, "ERROR: Empty response from API"
                
            # Look for the track data - handle both possible response structures
            track_data = None
            if 'knownGene' in data:
                track_data = data['knownGene']
            elif isinstance(data, list):
                track_data = data
                
            if not track_data:
                return None, "No gene data found in response"
            
            # Filter for genes that overlap our region
            overlapping_genes = []
            for gene in track_data:
                gene_start = gene.get('chromStart', 0)
                gene_end = gene.get('chromEnd', 0)
                
                # Check if our region falls within the gene's coordinates
                if gene_start <= end and gene_end >= start:
                    overlapping_genes.append(gene)
                    #print(f"\nFound overlapping gene: {gene}")  # Debug print
            
            if not overlapping_genes:
                return None, "No overlapping genes found"
                
            return overlapping_genes, None
            
        except requests.Timeout:
            return None, "ERROR: API request timed out"
        except requests.RequestException as e:
            return None, f"ERROR: API request failed: {str(e)}"
        except json.JSONDecodeError as e:
            return None, f"ERROR: Failed to parse API response: {str(e)}"
        except Exception as e:
            return None, f"ERROR: Unexpected error during API query: {str(e)}"

    # Parse coordinates
    loc, parse_error = parse_coords(cds)
    if parse_error:
        error_log.append(parse_error)
        return None, None, error_log
    
    # Query API
    genes, query_error = query_ucsc(loc['chr'], loc['start'], loc['end'])
    if query_error:
        error_log.append(query_error)
        return None, None, error_log

    if not genes:
        error_log.append(f"WARNING: No genes found for coordinates {loc['chr']}:{loc['start']}-{loc['end']}")
        return None, None, error_log

    # Get the most relevant gene (the one that best contains our region)
    best_gene = None
    best_overlap = 0
    region_length = loc['end'] - loc['start']
    
    for gene in genes:
        gene_start = gene.get('chromStart', 0)
        gene_end = gene.get('chromEnd', 0)
        
        # Calculate how much of our region is contained within this gene
        overlap_start = max(gene_start, loc['start'])
        overlap_end = min(gene_end, loc['end'])
        overlap = max(0, overlap_end - overlap_start)
        
        if overlap > best_overlap:
            best_overlap = overlap
            best_gene = gene

    if not best_gene:
        error_log.append("WARNING: Could not determine best matching gene")
        return None, None, error_log

    # Extract gene information 
    symbol = best_gene.get('geneName')
    if symbol == 'none' or symbol.startswith('ENSG'):
        # Try to find another gene with a proper symbol
        for gene in genes:
            potential_symbol = gene.get('geneName')
            if potential_symbol != 'none' and not potential_symbol.startswith('ENSG'):
                symbol = potential_symbol
                break

    name = get_gene_info(symbol) # Returns full human readable name

    if symbol == 'Unknown':
        error_log.append("WARNING: No symbol found in gene data")
    if name == 'Unknown':
        error_log.append("WARNING: No name found in gene data")

    # Update cache
    try:
        cache[cds] = {'symbol': symbol, 'name': name}
        with open(cache_file, 'w') as f:
            json.dump(cache, f)
    except Exception as e:
        error_log.append(f"WARNING: Failed to update cache file: {str(e)}")

    return symbol, name, error_log







def create_visualization(matrix_0, matrix_1, cds, result):
    """Create visualizations for a CDS analysis including special omega values."""

    gene_symbol, gene_name, error_log = get_gene_annotation(cds)
    
    # Print any errors or warnings that occurred
    if error_log:
        print(f"\nWarnings/Errors for CDS {cds}:")
        for msg in error_log:
            if msg.startswith("ERROR"):
                print(f"🚫 {msg}")
            elif msg.startswith("WARNING"):
                print(f"⚠️ {msg}")
            else:
                print(f"ℹ️ {msg}")
    
    # Handle the results
    if gene_symbol in [None, 'Unknown'] or gene_name in [None, 'Unknown']:
        print(f"❌ No annotation found for CDS: {cds}")
        gene_symbol = None  # Reset to None if Unknown
        gene_name = None    # Reset to None if Unknown
    else:
        print(f"✅ Found annotation:")
        print(f"   Symbol: {gene_symbol}")
        print(f"   Name: {gene_name}")

    # Read the original unfiltered data for the specific CDS
    df_all = pd.read_csv('all_pairwise_results.csv')
    df_cds_all = df_all[df_all['CDS'] == cds]

    # Create a pairwise dictionary including all omega values
    pairwise_dict_all = {(row['Seq1'], row['Seq2']): row['omega']
                         for _, row in df_cds_all.iterrows()}

    # Collect all unique sequences from both 'Seq1' and 'Seq2' columns, dropping NaN values
    sequences = pd.concat([df_cds_all['Seq1'], df_cds_all['Seq2']]).dropna().unique()

    sequences = [str(seq) for seq in sequences]

    # Create arrays for sequences in Group 0 and Group 1
    sequences_0 = np.array([seq for seq in sequences if not seq.endswith('1')])
    sequences_1 = np.array([seq for seq in sequences if seq.endswith('1')])

    # Recreate matrices including special omega values for visualization
    matrix_0_full, matrix_1_full = create_matrices(sequences_0, sequences_1, pairwise_dict_all)

    if matrix_0_full is None or matrix_1_full is None:
        print(f"No data available for CDS: {cds}")
        return

    # Prepare colormap for normal omega values
    # If omega = -1 → use light purple lavender color. If omega = 99 → use light red/pink color. If 0 ≤ omega ≤ 3 → map directly to color intensity (0→dark, 3→bright). If omega > 3 → treat as 3
    cmap_viridis = sns.color_palette("viridis", as_cmap=True)
    
    # Define colors for special omega values
    color_minus_one = (242/255, 235/255, 250/255)  # Very light lavender
    color_ninety_nine = (1, 192/255, 192/255)  # Very light red

    # Create a custom colormap by extending the viridis colormap with special colors
    colors = [color_minus_one] + cmap_viridis(np.linspace(0, 1, 252)).tolist() + [color_ninety_nine]
    new_cmap = ListedColormap(colors)
    
    # Define indices for special values
    special_minus_one_index = 0
    special_ninety_nine_index = len(colors) - 1

    # Now define special_patches using 'colors' and the indices
    special_patches = [
        mpatches.Patch(color=colors[special_minus_one_index], label='Identical sequences'),
        mpatches.Patch(color=colors[special_ninety_nine_index], label='No non-synonymous variation')
    ]

    def normalize_matrix(matrix):
        matrix_normalized = np.full_like(matrix, np.nan)
        
        # Create masks for different value types
        mask_minus_one = (matrix == -1)
        mask_ninety_nine = (matrix == 99)
        mask_normal = (~np.isnan(matrix)) & (~mask_minus_one) & (~mask_ninety_nine)
        
        # Special values get fixed indices
        matrix_normalized[mask_minus_one] = 0  # First color (light lavender)
        matrix_normalized[mask_ninety_nine] = 253  # Last color (light red)
        
        # Normal values (0-3) get mapped to indices 1-252
        normal_values = matrix[mask_normal]
        if normal_values.size > 0:
            # Cap values at 3.0
            capped_values = np.minimum(normal_values, 3.0)
            # Map 0->3 to 1->252
            mapped_values = 1 + (capped_values / 3.0 * 251)
            matrix_normalized[mask_normal] = mapped_values
        
        return matrix_normalized


    matrix_0_norm = normalize_matrix(matrix_0_full)
    matrix_1_norm = normalize_matrix(matrix_1_full)

    # Reset any custom font settings to defaults
    plt.rcParams.update(plt.rcParamsDefault)

    # Create a figure with specified size and layout
    fig = plt.figure(figsize=(16, 10))
    gs = plt.GridSpec(2, 3, height_ratios=[4, 1], hspace=0.6, wspace=0.6)

    # Main title
    fig.suptitle(f'Pairwise Comparison Analysis: {cds}', fontsize=18, fontweight='bold', y=0.95)

    if gene_symbol and gene_name:
        title = f'{gene_symbol}: {gene_name}\n{cds}'
    else:
        title = f'Pairwise Comparison Analysis: {cds}'
    fig.suptitle(title, fontsize=18, fontweight='bold', y=0.95)


    def plot_matrices(ax, matrix, title):
        """Plot matrix with special values in lower triangle, normal values in upper triangle.
        
        Args:
            ax: matplotlib axis to plot on
            matrix: normalized matrix (0-253 range where 0=omega -1, 253=omega 99, 1-252=omega 0-3)
            title: title for the plot
            
        The function automatically handles:
            - Special omega values (-1=0, 99=253)
            - Normal omega values (mapped to 1-252)
            - NaN values (masked/not shown)
            - Edge cases (all special, all normal, empty matrices)
            - Data validation and verification
            - Diagonal elements properly masked
        """
        # VALIDATION AND DEBUG PRINTS
        print("\n=== Matrix Analysis ===")
        print(f"Matrix shape: {matrix.shape}")
        print(f"Value range: {np.nanmin(matrix):.1f} to {np.nanmax(matrix):.1f}")
        print(f"Unique values: {sorted(np.unique(matrix[~np.isnan(matrix)]))}")
        print("Special value counts:")
        print(f"  Index 0 (omega=-1): {np.sum(matrix == 0)}")
        print(f"  Index 253 (omega=99): {np.sum(matrix == 253)}")
        print(f"Normal value count: {np.sum((matrix > 0) & (matrix < 253))}")
        print(f"NaN count: {np.sum(np.isnan(matrix))}")
    
        # Input validation
        if matrix.shape[0] != matrix.shape[1]:
            raise ValueError("Input matrix must be square")
        
        # Create base masked array with diagonal masked
        n = matrix.shape[0]
        diagonal_mask = np.eye(n, dtype=bool)
        
        # Start with a completely masked array
        final_matrix = np.ma.masked_all(matrix.shape)
        
        # Get indices for upper and lower triangles
        rows, cols = np.tril_indices(n, k=-1)  # Lower triangle
        upper_rows, upper_cols = np.triu_indices(n, k=1)  # Upper triangle
        
        # Create masks for special and normal values, excluding diagonal
        special_mask = ((matrix == 0) | (matrix == 253)) & ~diagonal_mask
        normal_mask = ((matrix > 0) & (matrix < 253)) & ~diagonal_mask
        
        # Verify masks are mutually exclusive
        overlap = special_mask & normal_mask
        if np.any(overlap):
            print("\nWARNING: Overlap detected between special and normal masks!")
            print(f"Overlap positions: {np.where(overlap)}")
            print("Values at overlap positions:", matrix[overlap])
        
        # VERIFICATION PRINTS
        print("\n=== Mask Verification ===")
        print(f"Special values (excluding diagonal): {np.sum(special_mask)}")
        print(f"Normal values (excluding diagonal): {np.sum(normal_mask)}")
        print(f"Diagonal elements: {np.sum(diagonal_mask)}")
        print(f"Total masked elements: {np.sum(final_matrix.mask)}")
        
        # Fill lower triangle with special values
        for i, j in zip(rows, cols):
            if special_mask[i, j]:
                final_matrix[i, j] = matrix[i, j]
        
        # Fill upper triangle with normal values
        for i, j in zip(upper_rows, upper_cols):
            if normal_mask[i, j]:
                final_matrix[i, j] = matrix[i, j]
        
        # FINAL VERIFICATION
        print("\n=== Final Matrix Verification ===")
        print("Lower triangle (special values):")
        lower_values = final_matrix[rows, cols]
        print(f"  Total filled values: {np.sum(~lower_values.mask)}")
        print(f"  Non-special values: {np.sum(~lower_values.mask & ((lower_values != 0) & (lower_values != 253)))}")
        
        print("\nUpper triangle (normal values):")
        upper_values = final_matrix[upper_rows, upper_cols]
        print(f"  Total filled values: {np.sum(~upper_values.mask)}")
        print(f"  Special values: {np.sum(~upper_values.mask & ((upper_values == 0) | (upper_values == 253)))}")
        
        # Verify diagonal is masked
        print("\nDiagonal verification:")
        diagonal_elements = np.diagonal(final_matrix)
        print(f"  Masked diagonal elements: {np.sum(diagonal_elements.mask)} (should be {n})")
        
        # Create the plot with verified data
        im = ax.imshow(final_matrix, origin='lower', interpolation='nearest', cmap=new_cmap)

        # Clean up plot
        ax.set_title(title, pad=10)
        ax.set_xticks([])
        ax.set_yticks([])

        # FINAL VISUAL VERIFICATION
        print("\n=== Visual Elements Verification ===")
        print(f"Plot limits: {ax.get_xlim()}, {ax.get_ylim()}")
        print(f"Matrix shape matches plot dimensions: {matrix.shape == (ax.get_xlim()[1] + 1, ax.get_ylim()[1] + 1)}")
        
        return im

    
    # Plot for Group 0
    ax1 = fig.add_subplot(gs[0, 0])
    plot_matrices(ax1, matrix_0_norm, f'Direct Sequence Matrix (n={len(sequences_0)})')

    # Plot for Group 1
    ax2 = fig.add_subplot(gs[0, 1])
    plot_matrices(ax2, matrix_1_norm, f'Inverted Sequence Matrix (n={len(sequences_1)})')

    # Distribution comparison between groups
    ax3 = fig.add_subplot(gs[0, 2])
    values_0 = matrix_0_full[np.tril_indices_from(matrix_0_full, k=-1)]
    values_1 = matrix_1_full[np.tril_indices_from(matrix_1_full, k=-1)]
    
    print("\n=== DEBUG: Histogram Data ===")
    print(f"Raw values_0 shape: {values_0.shape}")
    print(f"Raw values_1 shape: {values_1.shape}")
    print(f"Raw values_0 unique: {np.unique(values_0)}")
    print(f"Raw values_1 unique: {np.unique(values_1)}")
    
    # Exclude NaN, -1, and 99 values for plotting
    values_0 = values_0[~np.isnan(values_0)]
    values_0 = values_0[(values_0 != -1) & (values_0 != 99)]
    values_1 = values_1[~np.isnan(values_1)]
    values_1 = values_1[(values_1 != -1) & (values_1 != 99)]
    
    print(f"Filtered values_0 shape: {values_0.shape}")
    print(f"Filtered values_1 shape: {values_1.shape}")
    print(f"Filtered values_0 unique: {np.unique(values_0)}")
    print(f"Filtered values_1 unique: {np.unique(values_1)}")
    sns.kdeplot(values_0, ax=ax3, label='Group 0', fill=True, common_norm=False, color='#1f77b4', alpha=0.6)
    sns.kdeplot(values_1, ax=ax3, label='Group 1', fill=True, common_norm=False, color='#ff7f0e', alpha=0.6)
    ax3.set_title('Distribution of Omega Values', fontsize=14, pad=12)
    ax3.set_xlabel('Omega Value', fontsize=12)
    ax3.set_ylabel('Density', fontsize=12)
    ax3.tick_params(axis='both', which='major', labelsize=10)
    ax3.legend(title='Groups', title_fontsize=12, fontsize=10)

    # Results table
    ax4 = fig.add_subplot(gs[1, :])
    ax4.axis('off')

    # Prepare table data
    effect_size = f"{result['observed_effect_size']:.4f}" if not np.isnan(result['observed_effect_size']) else 'N/A'
    p_value = f"{result['p_value']:.4e}" if not np.isnan(result['p_value']) else 'N/A'
    std_err = f"{result['std_err']:.4f}" if not np.isnan(result['std_err']) else 'N/A'

    table_data = [
        ['Metric', 'Value'],
        ['Gene Symbol', gene_symbol if gene_symbol else 'Unknown'],
        ['Gene Name', gene_name if gene_name else 'Unknown'],
        ['Observed Effect Size (from Mixed Model)', effect_size],
        ['P-value', p_value],
    ]

    # Create table
    table = ax4.table(cellText=table_data, loc='center', cellLoc='left', colWidths=[0.5, 0.5])
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1, 1.5)

    # Style the table
    for (row, col), cell in table.get_celld().items():
        if row == 0:
            cell.set_text_props(weight='bold', ha='center')
            cell.set_facecolor('#E6E6E6')
        elif col == 0:
            cell.set_text_props(weight='bold')
        cell.set_edgecolor('gray')


    sm = plt.cm.ScalarMappable(cmap=cmap_viridis)

    sm.set_array([])

    # Add the colorbar
    # Move the colorbar next to the matrices
    # Get the position of ax2 (the second matrix plot)
    pos = ax2.get_position()
    
    # Adjust the position of the colorbar to be next to ax2
    cbar_ax = fig.add_axes([pos.x1 + 0.02, pos.y0, 0.02, pos.height])
    cbar = plt.colorbar(sm, cax=cbar_ax)
    cbar.set_label('Omega Value', fontsize=12)

    # Special values legend
    pos1 = ax1.get_position()
    pos2 = ax2.get_position()
    legend_x = pos1.x1 + (pos2.x0 - pos1.x1) / 2 - 0.08
    legend_y = pos1.y0 - 0.1
    legend_width = 0.16
    legend_height = 0.1
    
    legend_ax = fig.add_axes([legend_x, legend_y, legend_width, legend_height])
    legend_ax.axis('off')
    legend = legend_ax.legend(handles=special_patches, title='Special Values', 
                           loc='center', ncol=2, frameon=True, fontsize=10)
    legend.get_title().set_fontsize(12)

    plt.tight_layout(rect=[0, 0, 0.9, 0.95])

    # Save the figure
    plt.savefig(PLOTS_DIR / f'analysis_{cds.replace("/", "_")}.png',
                dpi=300, bbox_inches='tight')
    plt.close(fig)







def analyze_cds_parallel(args):
    """Analyze a single CDS"""
    df_cds, cds = args

    # Check cache first
    cached_result = load_cached_result(cds)
    if cached_result is not None:
        return cds, cached_result

    # Create pairwise dictionary
    pairwise_dict = {(str(row['Seq1']), str(row['Seq2'])): row['omega']
        for _, row in df_cds.iterrows()}

    # Collect all unique sequences from both 'Seq1' and 'Seq2' columns
    all_seqs = pd.concat([df_cds['Seq1'], df_cds['Seq2']]).unique()
    sequences_0 = np.array([seq for seq in all_seqs if not seq.endswith('1')])
    sequences_1 = np.array([seq for seq in all_seqs if seq.endswith('1')])
    all_sequences = np.concatenate([sequences_0, sequences_1])

    n0 = len(sequences_0)
    n1 = len(sequences_1)

    # Generate matrices for visualization (do this first)
    matrix_0, matrix_1 = create_matrices(sequences_0, sequences_1, pairwise_dict)

    # Set minimum required sequences per group
    min_sequences_per_group = 10

    # Initialize base result dictionary with matrices
    result = {
        'matrix_0': matrix_0,
        'matrix_1': matrix_1,
        'pairwise_comparisons': set(pairwise_dict.keys())
    }

    if n0 < min_sequences_per_group or n1 < min_sequences_per_group:
        # Not enough sequences in one of the groups
        result.update({
            'observed_effect_size': np.nan,
            'p_value': np.nan,
            'n0': n0,
            'n1': n1,
            'num_comp_group_0': 0,
            'num_comp_group_1': 0,
            'std_err': np.nan
        })
    else:
        # Call the analysis worker and update result
        worker_result = analysis_worker((
            all_sequences, n0, pairwise_dict,
            sequences_0, sequences_1
        ))
        result.update(worker_result)

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

def build_overlap_clusters(results_df): # test on longest instead
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
    """Combine statistics for a cluster of overlapping CDSs using the smallest p-value."""
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
    weighted_effect_size = 0.0
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

    if valid_cdss > 0:
        # Collect valid p-values
        valid_pvals = cluster_data.loc[valid_indices]['p_value'].values
    
        # Filter out invalid p-values
        valid_pvals = valid_pvals[~np.isnan(valid_pvals)]
        valid_pvals = valid_pvals[~np.isinf(valid_pvals)]
    
        if len(valid_pvals) > 0:
            # Normalized weights based on CDS lengths
            valid_weights = []
            for idx in valid_indices:
                cds = cluster_data.loc[idx, 'CDS']
                weight = weights.get(cds, 1 / len(cluster_cdss))
                valid_weights.append(weight)
            valid_weights = np.array(valid_weights)
            # Normalize weights so that they sum to 1
            valid_weights /= valid_weights.sum()
    
            # Combine p-values using Stouffer's method with weights
            z_stat, combined_p = combine_pvalues(valid_pvals, method='stouffer', weights=valid_weights)
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
    """Compute overall significance from independent clusters using scipy's combine_pvalues."""
    import numpy as np
    from scipy import stats

    # Initialize default return values
    overall_pvalue_combined = np.nan
    overall_effect = np.nan
    n_valid_clusters = 0
    total_comparisons = 0

    # Filter out clusters with valid combined_pvalue and weighted_effect_size
    valid_clusters = [
        c for c in cluster_results.values()
        if not np.isnan(c['combined_pvalue']) and not np.isnan(c['weighted_effect_size'])
    ]

    if valid_clusters:
        cluster_pvals = np.array([c['combined_pvalue'] for c in valid_clusters])

        # Use scipy.stats.combine_pvalues to combine p-values
        # Choose method: 'fisher', 'stouffer', or others as appropriate etc.
        statistic, overall_pvalue_combined = stats.combine_pvalues(cluster_pvals, method='fisher')

        print(f"\nCombined p-value using Fisher's method: {overall_pvalue_combined:.4e}")
        print(f"Fisher's statistic: {statistic:.4f}")

        # Stouffer's method
        weights = np.array([c['n_comparisons'] for c in valid_clusters], dtype=float)

        # Check for zero weights
        if np.all(weights == 0) or np.isnan(weights).any():
            weights = None
            print("Note: Weights not used in Stouffer's method due to zero or NaN values.")

        # Use Stouffer's method with weights
        statistic_stouffer, pvalue_stouffer = stats.combine_pvalues(cluster_pvals, method='stouffer', weights=weights)
        print(f"Combined p-value using Stouffer's method: {pvalue_stouffer:.4e}")
        print(f"Stouffer's Z-score statistic: {statistic_stouffer:.4f}")

        # Compute weighted effect size
        effect_sizes = np.array([c['weighted_effect_size'] for c in valid_clusters])

        if weights is not None:
            normalized_weights = weights / np.sum(weights)
        else:
            normalized_weights = np.ones_like(effect_sizes) / len(effect_sizes)

        overall_effect = np.average(effect_sizes, weights=normalized_weights)

        # Count comparisons
        all_unique_pairs = set()
        for c in valid_clusters:
            all_unique_pairs.update(c['cluster_pairs'])
        total_comparisons = len(all_unique_pairs)
        n_valid_clusters = len(valid_clusters)

        overall_pvalue = overall_pvalue_combined

    else:
        print("No valid clusters available for significance computation.")
        overall_pvalue = np.nan
        overall_pvalue_combined = np.nan
        overall_effect = np.nan
        pvalue_stouffer = np.nan

    return {
        'overall_pvalue': overall_pvalue,
        'overall_pvalue_fisher': overall_pvalue_combined,
        'overall_pvalue_stouffer': pvalue_stouffer,
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
        'overall_pvalue': float(overall_results['overall_pvalue']) if not np.isnan(overall_results['overall_pvalue']) else None,
        'overall_pvalue_fisher': float(overall_results['overall_pvalue_fisher']) if not np.isnan(overall_results['overall_pvalue_fisher']) else None,
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
    print(f"Overall effect size: {overall_results['overall_effect']:.4f}")

    # Sort results by p-value
    significant_results = results_df.sort_values('p_value')

    # Create visualizations for top significant results
    for _, row in significant_results.head(30).iterrows():
        cds = row['CDS']
        viz_result = results[cds]  # Get full result with matrices
        create_visualization(
            viz_result['matrix_0'],
            viz_result['matrix_1'],
            cds,
            row  # Use row for stats since they're the same
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
