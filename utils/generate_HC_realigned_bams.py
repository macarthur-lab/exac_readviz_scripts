"""
This script generates HC-reassembled bams. For each variant, it
selects which samples will be displayed and then runs HaplotypeCaller on those
samples. An unlimited number of instances of this script can be
run in parallel (for example in an array job) to speed up execution.
"""

# how many samples to show per het or hom-alt variant in the exac browser.
MAX_SAMPLES_TO_SHOW_PER_VARIANT = 5

# how many subdirectories to use to store the reassembled bams
NUM_OUTPUT_DIRECTORIES_L1 = 100
NUM_OUTPUT_DIRECTORIES_L2 = 10000

ACTIVE_REGION_PADDING = 300

import argparse
import collections
import datetime
import gzip
import os
import pysam
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import vcf

from postprocess_reassembled_bam import postprocess_bam

this_dir = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(this_dir, "../vcf"))

from minimal_representation import get_minimal_representation


def get_bam_path(sample_name, sample_name_to_bam_path):
    """Tries to find the bam for the given sample.
    Args:
      sample_name: vcf sample id
      sample_name_to_bam_path: based on exac info table
    Return: The bam path if the bam file exists.
    Raise:  IOError if the bam doesn't exist.
    """

    # work-arounds for relocated .bams based on @birndle's igv_spot_checking script
    bam_path = sample_name_to_bam_path[sample_name]
    if "/cga/pancan2/picard_bams/ext_tcga" in bam_path:
        bam_path = bam_path.replace("/cga/pancan2/", "/cga/fh/cga_pancan2/")

    if "CONT_" in bam_path:
        bam_path = bam_path.replace("CONT_", "CONT")

    bam_path = re.sub("/v[0-9]{1,2}/", "/current/", bam_path)  # get latest version of the bam

    if not os.access(bam_path, os.R_OK):
        raw_bam_path = sample_name_to_bam_path[sample_name]
        raise IOError("bam %s doesn't exist at %s" % (raw_bam_path, bam_path))

    return bam_path


def choose_samples(het_or_hom, alt_allele_index, genotypes, sample_name_include_status, sample_name_to_bam_path, sample_name_to_gvcf_path):
    """Contains heuristics for choosing which samples to display for a given
    variant (chrom, pos, ref, alt) in it's het or hom-alt state.

    Args:
        het_or_hom: Either "het" or "hom" to indicate whether to choose het or hom-alt samples.
        alt_allele_index: 1-based index of the alt allele that we're choosing samples for (bewteen 1 and n = total number of alt alleles at this site)
        genotypes: a dictionary that maps each sample_name to a 4-tuple: (gt_ref, gt_alt, GQ, DP)
            where gt_ref and gt_alt are integers between 0, and num alt alleles.
        sample_name_include_status: dictionary mapping sample_name to either
            True or False depending on the last column of the exac info table
        sample_name_to_bam_path: bam path from the exac info table

    Return:
        list of 2-tuples each of which represents a sample to display for this variant
        and contains 2 paths: (bam file path, gvcf path). The list length will be <= MAX_SAMPLES_TO_SHOW_PER_VARIANT.
    """
    assert het_or_hom in ["het", "hom"], "Unexpected het_or_hom arg: %s" % het_or_hom

    # filter down from all samples to just the samples that have the desired genotype and have include=YES
    relevant_samples = []  # a list of dicts
    for sample_name, (gt_ref, gt_alt, GQ, DP) in genotypes.items():
        if gt_ref is None and gt_alt is None:
            continue

        if het_or_hom == "het":
            if gt_ref == gt_alt:
                continue  # skip unless heterozygous
            if gt_ref != alt_allele_index and gt_alt != alt_allele_index:
                continue # check both gt_ref and gt_alt here to handle het genotypes that are alt-alt  (eg. 1/2)
        elif het_or_hom == "hom":
            if gt_ref != gt_alt:
                continue  # skip unless homozygous
            if gt_alt != alt_allele_index:
                continue  # skip unless homozygous for the specific alt allele we're looking for (this matters for multiallelics)
        else:
            raise ValueError("Unexpected het_or_hom value: " + het_or_hom)

        if DP < 10 or GQ < 20:
            continue  # ignore samples that don't pass _Adj thresholds since they are not counted in the ExAC browser het/hom counts.
        if sample_name_include_status[sample_name]:
            relevant_samples.append( {"sample_name": sample_name, "GQ": GQ} )


    # figure out list of bam paths for samples to display
    bams_to_display = []

    # get up to MAX_SAMPLES_TO_SHOW_PER_VARIANT samples with the highest GQ.
    # skip samples whose bams aren't found on disk.
    relevant_samples.sort(key=lambda s: s["GQ"], reverse=True)

    while relevant_samples and len(bams_to_display) < MAX_SAMPLES_TO_SHOW_PER_VARIANT:
        # retrieve the sample with the next-highest GQ, and remove it from the
        # list so its not considered again
        max_GQ_sample = relevant_samples[0]
        del relevant_samples[0]

        # try to convert sample name to bam path, but checking that the bam actually exists on disk
        try:
            sample_name = max_GQ_sample["sample_name"]
            bam_path = get_bam_path(sample_name, sample_name_to_bam_path)
            gvcf_path = sample_name_to_gvcf_path[sample_name]
            bams_to_display.append((bam_path, gvcf_path))
        except IOError as e:
            print("ERROR: %s" % e)

    return bams_to_display


def create_vcf_row_parser(header_line, valid_sample_names):
    """Defines and returns another function that can parse a single VCF row
    Args:
      header_line: The last line of the VCF header - the one that defines columns
      valid_sample_names: A set of valid sample names
    """
    header_fields = header_line.strip("\n").split("\t")

    assert header_fields[0] == "#CHROM" and header_fields[1] == "POS", \
        "Unexpected header_fields 1: %s" % str(header_fields[0:9])
    assert header_fields[3] == "REF" and header_fields[4] == "ALT", \
        "Unexpected header_fields 2: %s" % str(header_fields[0:9])
    assert header_fields[8] == "FORMAT", \
        "Unexpected header_fields 3: %s" % str(header_fields[0:9])

    # sanity check for sample_names
    sample_names = header_fields[9:]
    for sample_name in sample_names:
        if sample_name not in valid_sample_names:
            print("ERROR: vcf sample name '%s' is not in the vcf info table" % sample_name)

    expected_genotype_format = "GT:AD:DP:GQ:PL"
    GT_idx = expected_genotype_format.split(":").index("GT")
    GQ_idx = expected_genotype_format.split(":").index("GQ")
    DP_idx = expected_genotype_format.split(":").index("DP")

    def vcf_row_parser(fields):
        """Takes a tuple of column values from a VCF row and return a tuple of (chrom, pos, ref, alt_alleles, genotypes)
        where alt_alleles is a list of strings, and genotypes is a dictionary that maps sample_name to a 4-tuple:
        (gt_ref, gt_alt, GQ, DP)
        """
        chrom, pos, ref, alt = fields[0], fields[1], fields[3], fields[4].split(",")

        assert fields[8] == expected_genotype_format, \
            "Unexpected genotype format: '%s'. Expected: %s in %s" % (fields[8], expected_genotype_format, fields[0:8])

        genotypes = fields[9:]
        assert len(sample_names) == len(genotypes), \
            "Unexpected num sample names (%s) vs num genotypes (%s) in %s" % (len(sample_names), len(genotypes), fields[0:8])

        sample_name_to_genotype = {}
        for sample_name, genotype in zip(sample_names, genotypes):
            genotype_values = genotype.split(":")
            GT = genotype_values[GT_idx]
            if GT == "./.":
                gt_ref = gt_alt = None
                GQ = DP = None
            else:
                gt_ref, gt_alt = GT.split("/")
                try:
                    gt_ref = int(gt_ref)
                    gt_alt = int(gt_alt)

                except ValueError:
                    print("ERROR: Couldn't parse genotype %s in %s" % (GT, fields[0:8]))

                assert gt_ref <= len(alt) and gt_alt <= len(alt), "ERROR: Genotype numbers %s out of bounds in %s" % (GT, fields[0:8])

                try:
                    GQ = float(genotype_values[GQ_idx])
                    DP = float(genotype_values[DP_idx])
                except ValueError:
                    print("ERROR parsing %s genotype: %s in %s" % (sample_name, genotype, fields[0:8]))
                    # This error happens for genotypes like 1/1:0,0:.:6:70,6,0.
                    # set GQ, DP = 0 so this sample will be filtered out by the GQ<20,DP<10 filter
                    GQ = DP = 0

            sample_name_to_genotype[sample_name] = (gt_ref, gt_alt, GQ, DP)

        return chrom, pos, ref, alt, sample_name_to_genotype

    return vcf_row_parser


MAX_ALLELE_LENGTH = 75

def compute_reassembled_bam_path(base_dir, chrom, minrep_pos, minrep_ref, minrep_alt, het_or_hom, sample_i, suffix=""):
    """Returns a reassembled bam output path"""
    output_subdir = "%02d/%04d" % (minrep_pos % NUM_OUTPUT_DIRECTORIES_L1,
                                   minrep_pos % NUM_OUTPUT_DIRECTORIES_L2)
    output_bam_filename = "chr%s-%s-%s-%s_%s%s%s.bam" % (
        chrom,
        minrep_pos,
        minrep_ref[:MAX_ALLELE_LENGTH],
        minrep_alt[:MAX_ALLELE_LENGTH],
        het_or_hom,
        sample_i,
        suffix)

    return os.path.join(base_dir, output_subdir, output_bam_filename)


def run(shell_cmd, verbose=False):
    """Utility method to print and run a shell command"""
    if verbose:
        print(shell_cmd)
    return os.system(shell_cmd)


def create_symlink(sample_bam_path, sample_gvcf_path, output_bam_path):
    output_dir = os.path.dirname(output_bam_path)
    if not os.path.isdir(output_dir):
        run("mkdir -p %s" % output_dir)

    symlink_path = output_bam_path.replace(".bam", "") + ".original.bam"
    if os.path.isfile(symlink_path):
        return
    run("ln -s %s %s" % (sample_bam_path, symlink_path))
    run("ln -s %s %s" % (sample_bam_path.replace(".bam", ".bai"), symlink_path + ".bai"))

    symlink_path = output_bam_path.replace(".bam", "") + ".original.gvcf"
    if os.path.isfile(symlink_path):
        return
    run("ln -s %s %s" % (sample_gvcf_path, symlink_path))
    run("ln -s %s %s" % (sample_gvcf_path + ".tbi", symlink_path + ".tbi"))


def launch_haplotype_caller(region, reference_fasta_path, input_bam_path, output_bam_path):
    """Launch HC to compute the reassembled bam.

    Based on code from @birndle's igv_spot_checking script

    Args:
      region: string like "1:12345-54321"
      ...
    """

    output_dir = os.path.dirname(output_bam_path)
    if not os.path.isdir(output_dir):
        #print("Creating directory: " + output_dir)
        run("mkdir -p %s" % output_dir)

    # first output to a temp path to avoid leaving incompletely-generated .bams if HaplotypeCaller crashes or is killed
    temp_output_bam_path = os.path.join(output_dir, "tmp." + os.path.basename(output_bam_path))
    output_gvcf_path = output_bam_path.replace(".bam", "") + ".gvcf"

    # see https://www.broadinstitute.org/gatk/guide/article?id=5484  for details on using -bamout
    gatk_cmd = ['java',
                #'-jar', './gatk-protected/target/executable/GenomeAnalysisTK.jar',
                '-jar', '/seq/software/picard/current/3rd_party/gatk/GenomeAnalysisTK-3.1-144-g00f68a3.jar',
                '-T', 'HaplotypeCaller',
                '-R', reference_fasta_path,
                '--disable_auto_index_creation_and_locking_when_reading_rods',
                '-stand_call_conf', '30.0',
                '-stand_emit_conf', '30.0',
                '--minPruning', '3',
                '--maxNumHaplotypesInPopulation', '200',
                '-ERC', 'GVCF',
                '--max_alternate_alleles', '3',
                # '-A', 'DepthPerSampleHC',
                # '-A', 'StrandBiasBySample',
                '--variant_index_type', 'LINEAR',
                '--variant_index_parameter', '128000',
                '--paddingAroundSNPs', str(ACTIVE_REGION_PADDING),
                '--paddingAroundIndels', str(ACTIVE_REGION_PADDING),
                '--forceActive',
                '-I', input_bam_path,
                '-L', region,
                '-bamout', temp_output_bam_path,
                '--disable_bam_indexing',
                '-o', output_gvcf_path,
                #'-o', '/dev/null'
                ]

    try:
        print(" ".join(gatk_cmd))
        result = subprocess.check_output(gatk_cmd, stderr=subprocess.STDOUT)
        print("Done")
    except subprocess.CalledProcessError as e:
        print("ERROR: HC reassembly failed with error code %s." % e.returncode)
        print("   GATK output:\n%s" % e.output.strip())
    else:
        # strip out read groups, read ids, tags, etc. to remove any sensitive info and reduce bam size
        postprocess_bam(temp_output_bam_path, output_bam_path)

        run("samtools index %s" % output_bam_path)
        run("rm " + temp_output_bam_path)

def check_gvcf(original_gvcf_path, new_gvcf_path, original_chrom, minrep_pos, minrep_ref, minrep_alt, het_or_hom):
    """Checks whether the genotype that's in the gVCF generated by the HaplotypeCaller when
    computing the reassemled bam matches the genotype in the original gvcf. If not, this indicates a discrepency
    between the original ExAC run and the current run of HaplotypeCaller. The .gVCF file is deleted
    unless a discrepency is found, in which case it is left alone for further inspection.

    return: True if the check succeeded
    """
    if not os.path.isfile(new_gvcf_path):
        print("WARNING: new %s is missing. Can't check genotypes." % new_gvcf_path)
        return True

    for r_new in vcf.Reader(filename=new_gvcf_path):
        if r_new.CHROM == original_chrom and str(r_new.POS) == str(minrep_pos):
            break
    else:
        print("CHECK_GVCF:  FAIL: VARIANT NOT CALLED IN NEW GVCF: %(new_gvcf_path)s" % locals())
        return False

    # being here means a row in the new GVCF matched the chrom/pos of the ExAC call
    if not os.path.isfile(original_gvcf_path) or not os.path.isfile(original_gvcf_path + ".tbi"):
        print("WARNING: original %s (or %s.tbi) is missing. Can't check genotypes." % (original_gvcf_path, original_gvcf_path+".tbi"))
        return True

    f = vcf.Reader(filename=original_gvcf_path)
    r_original = f.fetch(r_new.CHROM, r_new.POS)

    def are_equal_calls(a,b):
        return a.CHROM == b.CHROM and a.POS == b.POS and a.REF == b.REF and a.ALT == b.ALT and (
            (not a.samples and not b.samples) or (a.samples and b.samples and a.samples[0]["GT"] == b.samples[0]["GT"]))

    print(str(r_original), r_original.samples[0])
    print(str(r_new), r_new.samples[0])

    are_equal = are_equal_calls(r_original, r_new)
    if not are_equal:
        print("CHECK_GVCF:  FAIL: calls not equal between %s and %s" % (original_gvcf_path, new_gvcf_path))
    print("-----")

    return are_equal



def parse_exac_calling_intervals(exac_calling_intervals_file, db_filename=":memory:"):
    """Parses the exac calling regions .intervals file into a sqlite database that has an
    'intervals' table with the columns: chrom, start_pos, end_pos, strand, target_name.

    Args:
      exac_calling_intervals_file: A path like "/seq/references/Homo_sapiens_assembly19/v1/variant_calling/exome_calling_regions.v1.interval_list"
      db_filename: The sqlite db filename. Use ":memory:" for an in-memory database.
    """
    print("Loading exac calling interals from %s" % exac_calling_intervals_file)
    in_memory_db = db_filename == ":memory:"
    if in_memory_db:
        db = sqlite3.connect(db_filename)
    else:
        # write to a tempfile to allow multi-threaded execution of this method without error
        temp_db_filename = os.path.join(tempfile.gettempdir(), "tmp.exac_calling_intervals_file.%s.db" % time.time())
        db = sqlite3.connect(temp_db_filename)

    db.execute("CREATE TABLE intervals(chrom text, start integer, end integer, strand text, name text)")
    db.execute("CREATE UNIQUE INDEX interval_idx1 ON intervals(chrom, start, end)")
    for line in open(exac_calling_intervals_file):
        if line.startswith("@"):
            continue  # skip header
        #fields are: chrom, start_pos, end_pos, strand, target_name
        fields = line.strip("\n").split("\t")
        db.execute("INSERT INTO intervals VALUES (?,?,?,?,?)", fields)
    db.commit()

    if not in_memory_db:
        db.close()
        if not os.path.isfile(db_filename):
            run("mv -f %s %s" % (temp_db_filename, db_filename))
        else:
            print("ERROR: couldn't move file. Destination %s already exists." % db_filename)
        db = sqlite3.connect(db_filename)

    # print some stats
    (intervals_count, min_size, max_size, mean_size,) = db.execute(
        "SELECT count(*), min(end-start), max(end-start), avg(end-start) from intervals").fetchone()
    print("Loaded %s intervals (range: %s to %s, mean: %s) into %s" % (
        intervals_count, min_size, max_size, int(mean_size), db_filename))

    return db

def main(args):
    """args: object returned by argparse.ArgumentParser.parse_args()"""

    # use exac info table to create in-memory mapping of sample_id => bam_path and sample_id => include
    sample_name_to_bam_path = {}
    sample_name_to_gvcf_path = {}   # original GVCF produced as part of the ExAC pipeline run
    sample_name_include_status = {}
    info_table_file = open(args.info_table)
    header = next(info_table_file)
    for line in info_table_file:
        # info table has columns: vcf_sampleID, sampleID, ProjectID, ProjectName, Consortium, gvcf, bam, Include
        fields = line.strip('\n').split('\t')
        include = fields[-1]
        vcf_sample_id = fields[0]
        bam_path = fields[-2]
        gvcf_path = fields[-3]
        assert vcf_sample_id not in sample_name_to_bam_path, "duplicate sample id: %s" % vcf_sample_id

        sample_name_to_bam_path[vcf_sample_id] = bam_path
        sample_name_to_gvcf_path[vcf_sample_id] = gvcf_path
        sample_name_include_status[vcf_sample_id] = True if include.upper() == "YES" else False

    # read the exac_calling_intervals into memory to pass to HaplotypeCaller
    #exac_calling_intervals_db_filename = "exac_calling_intervals.db"
    exac_calling_intervals_db_filename = ":memory:"
    if not os.path.isfile(exac_calling_intervals_db_filename):
        exac_calling_intervals_db = parse_exac_calling_intervals(args.exac_calling_intervals, exac_calling_intervals_db_filename)
    else:
        exac_calling_intervals_db = sqlite3.connect(exac_calling_intervals_db_filename)

    tabix_file = pysam.TabixFile(filename=args.full_vcf, parser=pysam.asTuple())
    last_header_line = list(tabix_file.header)[-1].decode("utf-8", "ignore")
    if args.chrom:
        vcf_iterator = tabix_file.fetch(args.chrom, 1, 10**10)  # use tabix to fetch 1 chromosome
    else:
        vcf_iterator = pysam.tabix_iterator(gzip.open(args.full_vcf), parser=pysam.asTuple())

    parse_vcf_row = create_vcf_row_parser(last_header_line, set(sample_name_include_status.keys()))

    # ok-to-be-public db - use this database to keep track of which variants have been completed
    db_table_columns = [
        "chrom text",
        "minrep_pos integer",
        "minrep_ref text",
        "minrep_alt text",

        "n_het integer",
        "n_hom_alt integer",
        "reassembled_bams_het text",  # comma-separated list of reassembled bam filenames for HET samples
        "reassembled_bams_hom text",  # comma-separated list of reassembled bam filenames for HOM samples

        "finished bool",

        #"start_date integer",
        #"finish_date integer",
        #"error text",
    ]

    db_filename_suffix = (("_chr%s" % args.chrom) if args.chrom else "") + (("_thread%s" % args.thread_i) if args.n_threads > 1 else "")

    temp_variants_db_path = os.path.join("/tmp", "exac_v3_variants%s.db" % db_filename_suffix) # use /tmp dir because SQLite doesn't work on NFS drives
    if os.path.isfile(temp_variants_db_path):
        os.remove(temp_variants_db_path)
    variants_db = sqlite3.connect(temp_variants_db_path) #, isolation_level=False)

    variants_db.execute("CREATE TABLE IF NOT EXISTS t(%s)" % ",".join(db_table_columns))
    variants_db.execute("CREATE UNIQUE INDEX IF NOT EXISTS variant_idx ON t(chrom, minrep_pos, minrep_ref, minrep_alt)")

    # private db - use this database to keep track of metadata that's for internal viewing only
    db2_table_columns = [
        "chrom text",
        "minrep_pos integer",
        "minrep_ref text",
        "minrep_alt text",

        "calling_region_start integer",
        "calling_region_end integer",

        "het_sample_names text",      # comma-separated HET sample names to show for this variant
        "hom_alt_sample_names text",  # comma-separated HOM-ALT sample names to show for this variant
    ]

    temp_variants_db2_path = os.path.join("/tmp", "exac_v3_private_metadata%s.db" % db_filename_suffix) # use /tmp dir because SQLite doesn't work on NFS drives
    if os.path.isfile(temp_variants_db2_path):
        os.remove(temp_variants_db2_path)
    variants_db2 = sqlite3.connect(temp_variants_db2_path) #, isolation_level=False)
    variants_db2.execute("CREATE TABLE IF NOT EXISTS t(%s)" % ",".join(db2_table_columns))
    variants_db2.execute("CREATE UNIQUE INDEX IF NOT EXISTS variant_idx ON t(chrom, minrep_pos, minrep_ref, minrep_alt)")

    counters = collections.defaultdict(int)
    for row in vcf_iterator:
        counters["    sites"] += 1
        if counters["    sites"] % args.n_threads != args.thread_i:
            # extremely simple way to allow parallelization by running
            # multiple instances of this script: just skip sites
            # where site_counter % num_threads != thread_i
            continue

        chrom, pos, ref, alt_alleles, genotypes = parse_vcf_row(row)

        for alt_allele_index, alt in enumerate(alt_alleles):
            counters["   all_alleles"] += 1

            # minrep
            minrep_pos, minrep_ref, minrep_alt = get_minimal_representation(pos, ref, alt)

            print("############")
            print("SITE: %(chrom)s:%(minrep_pos)s-%(minrep_ref)s-%(minrep_alt)s" % locals())

            # skip variants that are already finished
            #(already_finished,) = variants_db.execute(
            #    "SELECT count(*) FROM t "
            #    "WHERE chrom=? AND minrep_pos=? AND minrep_ref=? AND minrep_alt=? and finished=1", (
            #    chrom, minrep_pos, minrep_ref, minrep_alt)).fetchone()
            #if already_finished:
            #    continue

            counters["  alleles_to_be_added_to_db"] += 1

            if args.use_calling_intervals:
                # look up the exac calling interval that overlaps this variant, so it can be passed to HaplotypeCaller (-L arg)
                regions = list(exac_calling_intervals_db.execute(
                    "SELECT chrom, start, end FROM intervals WHERE chrom=? AND start<=? AND end>=?", (chrom, minrep_pos, minrep_pos)))

                assert len(regions) != 0, "No region overlaps variant %s-%s: %s" % (chrom, minrep_pos, regions)
                assert len(regions) < 2, "Multiple regions overlap variant %s-%s: %s" % (chrom, minrep_pos, regions)

                region_chrom, region_start, region_end = regions[0]
                suffix = ""
            else:
                region_chrom, region_start, region_end = chrom, minrep_pos - ACTIVE_REGION_PADDING, minrep_pos + ACTIVE_REGION_PADDING
                suffix = "__+-%s" % ACTIVE_REGION_PADDING

            region = "%s:%s-%s" % (region_chrom, region_start, region_end)

            # print some stats
            if counters["   all_alleles"] % 10 == 0:
                if counters["   all_alleles"] % 10 == 0:
                    run("cp %s %s" % (temp_variants_db_path, args.bam_output_dir), verbose=True)
                    run("cp %s %s" % (temp_variants_db2_path, args.bam_output_dir), verbose=True)
                print("%s: %s.  %s  %s" % (
                    str(datetime.datetime.now()).split(".")[0],
                    ", ".join(["%s=%s" % (k, v) for k,v in sorted(counters.items(), key=lambda kv: kv[0])]),
                    "-".join(map(str, (chrom, minrep_pos, minrep_ref, minrep_alt))),
                    region))

            # choose het and hom-alt samples to display
            chosen_samples = {}
            for het_or_hom in ["het", "hom"]:
                chosen_samples[het_or_hom] = choose_samples(
                    het_or_hom,
                    alt_allele_index + 1,  # add 1 since VCF genotypes count alt alleles starting from 1
                    genotypes,
                    sample_name_include_status,
                    sample_name_to_bam_path,
                    sample_name_to_gvcf_path)

            # skip variants where none of the samples were called het or hom-alt
            if len(chosen_samples["het"]) + len(chosen_samples["hom"]) == 0:
                print("No het or hom samples. Skipping this site...")
                counters[" hom_ref_alleles"] += 1
                continue

            # add variant to variants_db
            values = (chrom, minrep_pos, minrep_ref, minrep_alt,
                      len(chosen_samples["het"]),
                      len(chosen_samples["hom"]),
                      "", "", 0)
            question_marks = ",".join(["?"]*len(values))
            variants_db.execute("INSERT OR IGNORE INTO t VALUES ("+question_marks+")", values)
            variants_db.commit()

            # add variant to variants_db2
            values = (chrom, minrep_pos, minrep_ref, minrep_alt,
                      region_start,
                      region_end,
                      ",".join(map(lambda paths: paths[0], chosen_samples["het"])),
                      ",".join(map(lambda paths: paths[0], chosen_samples["hom"])),)
            question_marks = ",".join(["?"]*len(values))
            variants_db2.execute("INSERT OR IGNORE INTO t VALUES ("+question_marks+")", values)
            variants_db2.commit()

            counters["added_to_db"] += 1

            # run HaplotypeCaller to generated reassembled bam for each sample
            previously_seen_output_bam_paths = set()
            reassembled_bam_paths = collections.defaultdict(list)
            relative_reassembled_bam_paths = collections.defaultdict(list)
            for het_or_hom in ["het", "hom"]:
                chosen_samples_list = chosen_samples[het_or_hom]
                print("=======")
                print(str(len(chosen_samples_list)) + " chosen %(het_or_hom)s samples for %(chrom)s:%(minrep_pos)s-%(minrep_ref)s-%(minrep_alt)s: %(chosen_samples_list)s" % locals())
                for sample_i, (sample_bam_path, sample_gvcf_path) in enumerate(chosen_samples_list):
                    print("-----")
                    output_bam_path = compute_reassembled_bam_path(
                        args.bam_output_dir,
                        chrom,
                        minrep_pos,
                        minrep_ref,
                        minrep_alt,
                        het_or_hom,
                        sample_i,
                        suffix=suffix)

                    reassembled_bam_paths[het_or_hom].append(output_bam_path)

                    relative_output_bam_path = output_bam_path.replace(args.bam_output_dir+"/", "")
                    relative_reassembled_bam_paths[het_or_hom].append(relative_output_bam_path)

                    # sanity check - make sure bam path is not duplicate
                    if relative_output_bam_path in previously_seen_output_bam_paths:
                        print("ERROR: %s is not unique" % relative_output_bam_path)
                    else:
                        previously_seen_output_bam_paths.add(relative_output_bam_path)

                    if args.run_haplotype_caller:
                        if os.access(output_bam_path, os.R_OK):
                            print(("WARNING: reassembled bam already exists even though it's not marked "
                                "as finished in the database: %s. Will mark it as finished..") % output_bam_path)
                            continue

                        # run haplotype caller if reassembled bam doesn't exist yet
                        launch_haplotype_caller(
                            region,
                            args.fasta,
                            sample_bam_path,
                            output_bam_path)

                        new_gvcf_path = output_bam_path.replace(".bam", "")+".gvcf"
                        gvcfs_match = check_gvcf(sample_gvcf_path, new_gvcf_path, chrom, minrep_pos, minrep_ref, minrep_alt, het_or_hom)
                        if gvcfs_match:
                            if os.path.isfile(new_gvcf_path):
                                os.remove(new_gvcf_path)
                            if os.path.isfile(new_gvcf_path + ".idx"):
                                os.remove(new_gvcf_path + ".idx")

                        counters["bam_generated"] += 1

                    if args.create_links_to_original_bams or not gvcfs_match:
                        create_symlink(sample_bam_path, sample_gvcf_path, output_bam_path)

            if args.take_igv_screenshots:
                print("Taking igv screenshots")
                import igv_api
                #igv_jar_path="/home/unix/mlek/bin/IGV_plotter/IGV_2.0.35/igv.jar"
                igv_jar_path="~/bin/igv-2.3.57/igv.jar"
                r = igv_api.IGVCommandLineRobot(verbose=True,
                                                igv_window_width=1200,
                                                igv_window_height=1600,
                                                igv_jar_path=igv_jar_path)

                for het_or_hom in ["het", "hom"]:
                    r.new_session()
                    r.max_panel_height(2000)
                    original_bams = map(lambda paths: paths[0], chosen_samples[het_or_hom])
                    for original_bam, reassembled_bam in zip(original_bams, reassembled_bam_paths[het_or_hom]):
                        print("%s vs %s" % (original_bam, reassembled_bam))
                    r.load(list(sum(zip(original_bams, reassembled_bam_paths[het_or_hom]), ())))
                    r.load(["./gencode.v19.annotation.gtf.gz",
                            "./exome_calling_regions.v1.bed.gz"])
                    r.goto("%s:%s-%s" % (region_chrom, minrep_pos - 205, minrep_pos + 195))
                    output_png_path = os.path.join(os.path.dirname(output_bam_path), os.path.basename(output_bam_path).split("_")[0]+"_"+het_or_hom+suffix+".png")
                    r.screenshot(output_png_path)
                    r.exit_igv()
                    r.execute()

            # update variants_db
            variants_db.execute("UPDATE t SET "
                "finished=1, reassembled_bams_het=?, reassembled_bams_hom=?"
                "WHERE "
                "chrom=? AND minrep_pos=? AND minrep_ref=? AND minrep_alt=?", (
                    ",".join(relative_reassembled_bam_paths["het"]),
                    ",".join(relative_reassembled_bam_paths["hom"]),
                    chrom, minrep_pos, minrep_ref, minrep_alt))
            variants_db.commit()

    variants_db.close()

    print("Finished.")
    run("cp %s %s" % (temp_variants_db_path, args.bam_output_dir), verbose=True)
    run("cp %s %s" % (temp_variants_db2_path, args.bam_output_dir), verbose=True)



if __name__ == "__main__":
    p = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--info-table", help="Path of ExAC info table",
        default='/humgen/atgu1/fs03/lek/resources/ExAC/ExAC.r0.3_meta_Final.tsv')
    p.add_argument("--full-vcf", help="Path of the ExAC full vcf with genotypes",
        default='/humgen/atgu1/fs03/konradk/exac/gqt/exac_all.vcf.gz')
    p.add_argument("-R", "--fasta", help="Reference genome hg19 fasta",
        default="/seq/references/Homo_sapiens_assembly19/v1/Homo_sapiens_assembly19.fasta")
    p.add_argument("--bam-output-dir", help="Where to output HC-reassembled bams",
        default='/broad/hptmp/exac_readviz_backend/')
    p.add_argument("--exac-calling-intervals", help="ExAC calling regions .intervals file",
        default="/seq/references/Homo_sapiens_assembly19/v1/variant_calling/exome_calling_regions.v1.interval_list")
    p.add_argument("-i", "--thread-i", help="Thread number (must be between 1 and the value of --n-threads)", default=1, type=int)
    p.add_argument("-n", "--n-threads", help="Total number of threads", default=1, type=int)
    p.add_argument("--take-igv-screenshots", help="Whether to take IGV snapshots", action="store_true")
    p.add_argument("--run-haplotype-caller", help="Whether to run haplotype caller", action="store_true")
    p.add_argument("--create-links-to-original-bams", help="Whether to create symlinks to the original bams for each sample for debugging", action="store_true")
    #p.add_argument("--use-calling-intervals", help="Whether to pass the ExAC calling region to -L when running HaplotypeCaller", action="store_true")
    p.add_argument("chrom", nargs="?", help="If specified, only data for this chromosome will be added to the database.")
    args = p.parse_args()

    args.use_calling_intervals = True # use them by default

    # print out settings
    print("Running with settings: ")
    for argname in filter(lambda n: not n.startswith("_"), dir(args)):
        print("   %s = %s" % (argname, getattr(args, argname)))
    print("\n")

    # validate args
    i = 0
    while True:
        try:
            assert os.path.isfile(args.info_table), "Couldn't find: %s" % args.info_table
            assert os.path.isfile(args.full_vcf), "Couldn't find: %s" % args.full_vcf
            assert os.path.isfile(args.full_vcf+".tbi"), "Couldn't find: %s.tbi" % args.full_vcf
            assert os.path.isdir(args.bam_output_dir), "Couldn't find: %s" % args.bam_output_dir
            assert os.path.isfile(args.exac_calling_intervals), "Couldn't find: %s" % args.exac_calling_intervals

            assert args.thread_i > 0, "Invalid --thread_i arg (%s) or --n-threads arg (%s)" % (args.thread_i, args.n_threads)
            assert args.thread_i <= args.n_threads, "Invalid --thread_i arg (%s) or --n-threads arg (%s)" % (args.thread_i, args.n_threads)
            break
        except AssertionError:
            if i >= 5:
                raise
            i += 1  # retry logic
            time.sleep(1)


    if args.chrom:
        args.chrom = args.chrom.replace("chr", "").upper()
        if args.chrom not in list(map(str, range(1, 23))) + ["X", "Y", "M", "MT"]:
            p.error("Invalid chromosome: " + args.chrom)
        print("Thread #%s out of %s will run on chr%s" % (args.thread_i, args.n_threads, args.chrom))
    else:
        print("Thread #%s out of %s will run on all chromosomes" % (args.thread_i, args.n_threads))

    args.thread_i = args.thread_i - 1  # convert to a 0-based count
    main(args)