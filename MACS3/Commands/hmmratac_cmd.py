# Time-stamp: <2022-06-03 14:49:00 Tao Liu>

"""Description: Main HMMR command

This code is free software; you can redistribute it and/or modify it
under the terms of the BSD License (see thefile LICENSE included with
the distribution).
"""

# ------------------------------------
# python modules
# ------------------------------------

from logging.handlers import RotatingFileHandler
import os
import sys
import logging
import numpy as np
#from typing import Sized

# ------------------------------------
# own python modules
# ------------------------------------
from MACS3.Utilities.Constants import *
from MACS3.Utilities.OptValidator import opt_validate_hmmratac
from MACS3.IO.PeakIO import PeakIO
from MACS3.IO.PeakIO import BroadPeakIO
from MACS3.IO.Parser import BAMPEParser #BAMaccessor
from MACS3.Signal.HMMR_EM import HMMR_EM
from MACS3.Signal.HMMR_Signal_Processing import generate_weight_mapping, generate_digested_signals, extract_signals_from_regions
from MACS3.Signal.HMMR_HMM import hmm_training, hmm_predict
from MACS3.Signal.Region import Regions
from MACS3.Signal.BedGraph import bedGraphTrackI

#from MACS3.IO.BED import BEDreader # this hasn't been implemented yet.

# ------------------------------------
# constants
# ------------------------------------

# ------------------------------------
# Misc functions
# ------------------------------------

# ------------------------------------
# Main function
# ------------------------------------
def run( args ):
    """The HMMRATAC function/pipeline for MACS.

    """
    #############################################
    # 1. Read the input BAM files
    #############################################
    options = opt_validate_hmmratac( args )
    options.info("\n" + options.argtxt)    
    options.info("#1 Read fragments from BAM file...")

    bam = BAMPEParser(options.bam_file[0], buffer_size=options.buffer_size)
    petrack = bam.build_petrack()
    if len( options.bam_file ) > 1:
        # multiple input
        for bamfile in options.bam_file[1:]:
            bam = BAMPEParser(bamfile, buffer_size=options.buffer_size)
            petrack = bam.append_petrack( petrack )
    # remember to finalize the petrack
    petrack.finalize()

    # filter duplicates if needed
    if options.misc_keep_duplicates:
        petrack.filter_dup( maxnum=1 )

    # read in blacklisted if option entered    
    if options.blacklist:
        options.info("#  Read blacklist file...")
        peakio = open( options.blacklist )
        blacklist = PeakIO()
        i = 0
        for l in peakio:
            fs = l.rstrip().split()
            i += 1
            blacklist.add( fs[0].encode(), int(fs[1]), int(fs[2]), name=b"%d" % i )
            blacklist.sort()
        blacklist_regions = Regions()
        blacklist_regions.init_from_PeakIO( blacklist )

    #############################################
    # 2. EM
    #############################################
    if options.em_skip:
        # Skip EM and use the options.em_means and options.em_stddevs
        em_means = options.em_means
        em_stddevs = options.em_stddevs
        options.info( "#2 EM is skipped. The following means and stddevs will be used:" )
    else:
        # we will use EM to get the best means/stddevs for the mono-, di- and tri- modes of fragment sizes
        options.info("#2 Use EM algorithm to estimate means and stddevs of fragment lengths")
        options.info("#  for mono-, di-, and tri-nucleosomal signals...") 
        em_trainer = HMMR_EM( petrack, options.em_means[1:4], options.em_stddevs[1:4], seed = options.hmm_randomSeed )
        # the mean and stddev after EM training
        em_means = [options.em_means[0],]
        em_means.extend(em_trainer.fragMeans)
        em_stddevs = [options.em_stddevs[0],]
        em_stddevs.extend(em_trainer.fragStddevs)    
        options.info( f"#  The means and stddevs after EM:")
    options.info(     f"#            [short,  mono-,  di-,  tri-]")
    options.info(     f"#   Means:   {em_means}")
    options.info(     f"#   Stddevs: {em_stddevs}")

    #############################################
    # 3. Define training set by peak calling
    #############################################

    # Find regions with fold change within determined range to use as training sites.
    # Find regions with zscore values above certain cutoff to exclude from viterbi.
    # 
    options.info( f"#3 Looking for training set from {petrack.total} fragments" )
    options.info( f"#  Pile up all fragments" )
    fc_bdg = petrack.pileup_bdg( [1.0,], baseline_value = 0 )
    (sum_v, n_v, max_v, min_v, mean_v, std_v) = fc_bdg.summary()
    options.info( f"#  Convert pileup to fold-change over average signal" )
    fc_bdg.apply_func(lambda x: x/mean_v)
    minlen = int(petrack.average_template_length)
    options.info( f"#  Call peak above within fold-change range of {options.hmm_lower} and {options.hmm_upper}." )
    options.info( f"#   The minimum length of the region is set as the average template/fragment length in the dataset: {minlen}" )
    options.info( f"#   The maximum gap to merge nearby significant regions into one is set as the flanking size to extend training regions: {options.hmm_training_flanking}" )    
    peaks = fc_bdg.call_peaks (cutoff=options.hmm_lower, min_length=minlen, max_gap=options.hmm_training_flanking, call_summits=False)
    f = open("a.bed","w")
    peaks.write_to_bed( f )
    options.info( f"#  Total training regions called: {peaks.total}" )
    peaks.filter_score( options.hmm_lower, options.hmm_upper )
    options.info( f"#  Total training regions after filtering with lower and upper cutoff: {peaks.total}" )
    
    if peaks.total > options.hmm_maxTrain:
        peaks = peaks.randomly_pick( options.hmm_maxTrain, seed = options.hmm_randomSeed )
        options.info( f"#  We randomly pick {options.hmm_maxTrain} regions for training" )

    # Now we convert PeakIO to Regions and filter blacklisted regions
    training_regions = Regions()
    training_regions.init_from_PeakIO( peaks )
    # We will expand the regions to both directions and merge overlap
    options.info( f"#  We expand the training regions with {options.hmm_training_flanking} and merge overlap" )
    training_regions.expand( options.hmm_training_flanking )
    training_regions.merge_overlap()
    
    # remove peaks overlapping with blacklisted regions
    if options.blacklist:
        training_regions.exclude( blacklist_regions )
        options.info( f"#  after removing those overlapping with provided blacklisted regions, we have {training_regions.total} left" )

    if ( options.print_train ):
        fhd = open(options.name+"_training_regions.bed","w")
        training_regions.write_to_bed( fhd )
        fhd.close()
        options.info( f"#  Training regions have been saved to `{options.name}_training_regions.bed` " )

    #############################################
    # 4. Train HMM
    #############################################
    options.info( f"#4 Train Hidden Markov Model with Gaussian Emission" )
    options.info( f"#  Compute the weights for each fragment length for each of the four signal types")
    fl_dict = petrack.count_fraglengths()
    fl_list = list(fl_dict.keys())
    fl_list.sort()
    # now we will prepare the weights for each fragment length for
    # each of the four distributions based on the EM results
    weight_mapping = generate_weight_mapping( fl_list, em_means, em_stddevs )
    
    options.info( f"#  Generate short, mono-, di-, and tri-nucleosomal signals")
    digested_atac_signals = generate_digested_signals( petrack, weight_mapping )
    
    # options.info( f"#  Saving short, mono-, di-, and tri-nucleosomal signals to bedGraph files")
    
    # fhd = open(options.oprefix+"_short.bdg","w")
    # digested_atac_signals[ 0 ].write_bedGraph(fhd, "short","short")
    # fhd.close()

    # fhd = open(options.oprefix+"_mono.bdg","w")
    # digested_atac_signals[ 1 ].write_bedGraph(fhd, "mono","mono")
    # fhd.close()
    
    # fhd = open(options.oprefix+"_di.bdg","w")
    # digested_atac_signals[ 2 ].write_bedGraph(fhd, "di","di")
    # fhd.close()
    
    # fhd = open(options.oprefix+"_tri.bdg","w")
    # digested_atac_signals[ 3 ].write_bedGraph(fhd, "tri","tri")
    # fhd.close()

    # We first bin the training regions then get four types of signals
    # in the bins, at the same time, we record how many bins for each
    # peak.
    options.info( f"#  Extract signals in training regions with bin size of {options.hmm_binsize}")
    [ training_bins, training_data, training_data_lengths ] = extract_signals_from_regions( digested_atac_signals, training_regions, binsize = options.hmm_binsize )

    if options.print_train:
        f = open(options.name+"_training_data.txt","w")
        for v in training_data:
            f.write( f"{v[0]}\t{v[1]}\t{v[2]}\t{v[3]}\n" )
        f.close()
    
    #f = open(options.name+"_training_lens.txt","w")
    #for v in training_data_lengths:
    #    f.write( f"{v}\n" )
    #f.close()
    
    options.info( f"#  Use Baum-Welch algorithm to train the HMM")
    hmm_model = hmm_training( training_data, training_data_lengths, random_seed = options.hmm_randomSeed )

    # label hidden states
    i_open_region = np.where(hmm_model.means_ == max(hmm_model.means_[0:3,0]))[0][0]
    i_background_region = np.where(hmm_model.transmat_ == min(hmm_model.transmat_[0:3, i_open_region]))[0][0]
    i_nucleosomal_region = list(set([0, 1, 2]) - set([i_open_region, i_background_region]))[0]

    f = open(options.name+"_model.txt","w")
    f.write( str(hmm_model.startprob_)+"\n" )
    f.write( str(hmm_model.transmat_ )+"\n" )
    f.write( str(hmm_model.means_ )+"\n" )
    f.write( str(hmm_model.covars_ )+"\n" )

    f.write( 'open region = state ' + str(i_open_region)+"\n" )
    f.write( 'nucleosomal region = state ' + str(i_nucleosomal_region)+"\n" )
    f.write( 'background region = state ' + str(i_background_region)+"\n" )

    f.close()

#############################################
# 5. Predict
#############################################
    # Our prediction strategy will be different with HMMRATAC, we will first ask MACS call peaks with loose cutoff, then for each peak we will run HMM prediction to figure out labels. And for the rest of genomic regions, just mark them as 'background'.
    options.info( f"#5 Decode with Viterbi to predict states" )    
    candidate_peaks = fc_bdg.call_peaks (cutoff=options.hmm_lower/2, min_length=minlen, max_gap=options.hmm_training_flanking, call_summits=False)
    options.info( f"#5  Total candidate peaks : {candidate_peaks.total}" )


    # Now we convert PeakIO to Regions and filter blacklisted regions
    candidate_regions = Regions()
    candidate_regions.init_from_PeakIO( candidate_peaks )
    # We will expand the regions to both directions and merge overlap
    options.info( f"#  We expand the candidate regions with {options.hmm_training_flanking} and merge overlap" )
    candidate_regions.expand( options.hmm_training_flanking )
    candidate_regions.merge_overlap()
    
    # remove peaks overlapping with blacklisted regions
    if options.blacklist:
        candidate_regions.exclude( blacklist_regions )
        options.info( f"#  after removing those overlapping with provided blacklisted regions, we have {candidate_regions.total} left" )

    # extract signals
    options.info( f"#  Extract signals in candidate regions")
    # Note: we can implement in a different way to extract then predict for each candidate region.
    [ candidate_bins, candidate_data, candidate_data_lengths ] = extract_signals_from_regions( digested_atac_signals, candidate_regions, binsize = options.hmm_binsize )
    
    options.info( f"#  Use HMM to predict states")
    predicted_proba = hmm_predict( candidate_data, candidate_data_lengths, hmm_model )

#############################################
# 6. Output - add to OutputWriter
#############################################
    options.info( f"# Write the output...")
    # Now taken the candidate_bins and predicted_proba, we can generate various
    # outputs
    
    # One thing to remember about candidate_bins is that the position
    # in this array is the 'end' of the bin, the actual region is the
    # 'end'-'binsize' to the 'end'.
    
    # First, the likelihoods for each of the three states in a bedGraph
    if options.save_likelihoods:
        options.info( f"# Write the likelihoods for each states into three bedGraph files {options.name}_open.bdg, {options.name}_nuc.bdg, and {options.name}_bg.bdg")
        open_state_bdg_file = open( options.name+"_open.bdg","w" )
        nuc_state_bdg_file = open( options.name+"_nuc.bdg","w" )
        bg_state_bdg_file = open( options.name+"_bg.bdg","w" )
        save_proba_to_bedGraph( candidate_bins, predicted_proba, options.hmm_binsize, open_state_bdg_file, nuc_state_bdg_file, bg_state_bdg_file, i_open_region, i_nucleosomal_region, i_background_region )
        open_state_bdg_file.close()
        nuc_state_bdg_file.close()
        bg_state_bdg_file.close()
    
    # Generate states path:
    states_path = generate_states_path( candidate_bins, predicted_proba, options.hmm_binsize, i_open_region, i_nucleosomal_region, i_background_region )
    
    # Save states path if needed
    # PS: we need to implement extra feature to include those regions NOT in candidate_bins and assign them as 'background state'.
    if options.save_states:
        options.info( f"# Write states assignments in a BED file: {options.name}_states.bed" )
        f = open(options.name+"_states.bed","w")
        save_states_bed( states_path, f )
        f.close()

    options.info( f"# Write accessible regions in a gappedPeak file: {options.name}_accessible_regions.gappedPeak")
    ofhd = open(options.name+"_accessible_regions.gappedPeak","w")
    save_accessible_regions( states_path, ofhd )
    ofhd.close()

def save_proba_to_bedGraph( candidate_bins, predicted_proba, binsize, open_state_bdg_file, nuc_state_bdg_file, bg_state_bdg_file, i_open, i_nuc, i_bg ):
    open_state_bdg = bedGraphTrackI( baseline_value = 0 )
    nuc_state_bdg = bedGraphTrackI( baseline_value = 0 )
    bg_state_bdg = bedGraphTrackI( baseline_value = 0 )

    prev_chrom_name = None
    prev_bin_end = None
    for l in range(len(predicted_proba)):
        # note that any region not in the candidate bins will be
        # treated as absolutely the background
        chrname = candidate_bins[l][0]
        end_pos = candidate_bins[l][1]
        start_pos = end_pos - binsize
        if chrname != prev_chrom_name:
            prev_chrom_name = chrname
            # add the first region as background
            if start_pos > 0:
                open_state_bdg.add_loc( chrname, 0, start_pos, 0.0 )
                nuc_state_bdg.add_loc( chrname, 0, start_pos, 0.0 )
                bg_state_bdg.add_loc( chrname, 0, start_pos, 1.0 )
                prev_bin_end = start_pos
            elif start_pos == 0:
                # if start_pos == 0, then the first bin has to be assigned, we set prev_bin_end as 0 
                prev_bin_end = 0
        # now check if the prev_bin_end is start_pos, if not, add a gap of background
        if prev_bin_end < start_pos:
            open_state_bdg.add_loc( chrname, prev_bin_end, start_pos, 0.0 )
            nuc_state_bdg.add_loc( chrname, prev_bin_end, start_pos, 0.0 )
            bg_state_bdg.add_loc( chrname, prev_bin_end, start_pos, 1.0 )

        open_state_bdg.add_loc( chrname, start_pos, end_pos, predicted_proba[l][ i_open ] )
        nuc_state_bdg.add_loc( chrname, start_pos, end_pos, predicted_proba[l][ i_nuc ] )
        bg_state_bdg.add_loc( chrname, start_pos, end_pos, predicted_proba[l][ i_bg ] )
        prev_bin_end = start_pos

    open_state_bdg.write_bedGraph( open_state_bdg_file, "Open States", "Likelihoods of being Open States" )
    nuc_state_bdg.write_bedGraph( nuc_state_bdg_file, "Nucleosomal States", "Likelihoods of being Nucleosomal States" )
    bg_state_bdg.write_bedGraph( bg_state_bdg_file, "Background States", "Likelihoods of being Background States" )
    return

def save_states_bed( states_path, states_bedfile ):
    # we do not need to output background state. 
    for l in range( len( states_path ) ):
        if states_path[l][3] != "bg":
            states_bedfile.write( "%s\t%s\t%s\t%s\n" % states_path[l] )
    return

def generate_states_path( candidate_bins, predicted_proba, binsize, i_open_region, i_nucleosomal_region, i_background_region ):
    ret_states_path = []
    labels_list = ["open","nuc","bg"]
    start_pos = candidate_bins[0][1]-binsize
    for l in range(1, len(predicted_proba)):
        proba_prev = np.array([ predicted_proba[l-1][ i_open_region ], predicted_proba[l-1][ i_nucleosomal_region ], predicted_proba[l-1][ i_background_region ] ])
        label_prev = labels_list[ np.argmax(proba_prev) ]
        proba_curr = np.array([ predicted_proba[l][ i_open_region ], predicted_proba[l][ i_nucleosomal_region ], predicted_proba[l][ i_background_region ] ])
        label_curr = labels_list[ np.argmax(proba_curr) ]
        if label_prev != label_curr:
            end_pos = candidate_bins[l-1][1]
            ret_states_path.append( (candidate_bins[l][0].decode(), start_pos, end_pos, label_prev) )
            start_pos = candidate_bins[l][1]-binsize
        elif l == len(predicted_proba)-1:
            end_pos = candidate_bins[l][1]
            ret_states_path.append( (candidate_bins[l][0].decode(), start_pos, end_pos, label_prev) )
    return ret_states_path

def save_accessible_regions( states_path, accessible_region_file ):
    # select only accessible regions from _states.bed, look for nuc-open-nuc pattern
    # This by default is the only final output from HMMRATAC
    accessible_regions = []
    for i in range(len(states_path)-1):
        if states_path[i][3] == 'nuc' and states_path[i+1][3] == 'open' and states_path[i+2][3] == 'nuc':
            if len(accessible_regions) > 0:  
                if int(states_path[i][2]) == int(accessible_regions[-1][2]): #if element already in list, don't repeat
                    accessible_regions.append((states_path[i+1][0], int(states_path[i+1][1]), int(states_path[i+1][2]), states_path[i+1][3]))
                    accessible_regions.append((states_path[i+2][0], int(states_path[i+2][1]), int(states_path[i+2][2]), states_path[i+2][3]))
                else:
                    accessible_regions.append((states_path[i][0], int(states_path[i][1]), int(states_path[i][2]), states_path[i][3]))
                    accessible_regions.append((states_path[i+1][0], int(states_path[i+1][1]), int(states_path[i+1][2]), states_path[i+1][3]))
                    accessible_regions.append((states_path[i+2][0], int(states_path[i+2][1]), int(states_path[i+2][2]), states_path[i+2][3]))
            elif len(accessible_regions) == 0:
                accessible_regions.append((states_path[i][0], int(states_path[i][1]), int(states_path[i][2]), states_path[i][3]))
                accessible_regions.append((states_path[i+1][0], int(states_path[i+1][1]), int(states_path[i+1][2]), states_path[i+1][3]))
                accessible_regions.append((states_path[i+2][0], int(states_path[i+2][1]), int(states_path[i+2][2]), states_path[i+2][3]))
    
    # group states by region
    list_of_groups = []
    one_group = []
    one_group.append(accessible_regions[0])
    for j in range(1, len(accessible_regions)):
        # future: add gap here ... if accessible_regions[j][1] == accessible_regions[j-1][2]+options.hmm_binsize or +options.gap 
        # (this will need to be addressed in how _states.bed and the nuc-open-nuc pattern is selected)
        if accessible_regions[j][1] == accessible_regions[j-1][2]:
            one_group.append(accessible_regions[j])
        elif accessible_regions[j][1] != accessible_regions[j-1][2]:
            list_of_groups.append(one_group)
            one_group = []
            one_group.append(accessible_regions[j])
    accessible_regions = list_of_groups

    # generate broadpeak object
    broadpeak = BroadPeakIO()
    for i in range(len(accessible_regions)-1):
        block_num = sum('open' in tup for tup in accessible_regions[i]) #number of open states in the region
        block_sizes = ''
        block_starts = ''
        for j in range(1, len(accessible_regions[i])-1, 2):
            block_sizes = block_sizes + str(accessible_regions[i][j][2] - accessible_regions[i][j][1]) + ',' #distance between start_pos and end_pos in each open state
            block_starts = block_starts + str(accessible_regions[i][j][1] - accessible_regions[i][0][1] ) + ',' #start_pos for each open state, it's relative position to the start_pos of the whole broad region

        broadpeak.add(bytes(accessible_regions[i][1][0], encoding="raw_unicode_escape"), #chromosome
            accessible_regions[i][0][1], #start_pos of first nuc state in the region
            accessible_regions[i][-1][2], #end_pos of the last nuc state in the region
            thickStart=bytes(str(accessible_regions[i][1][1]), encoding="raw_unicode_escape"), #start_pos of the first open state
            thickEnd=bytes(str(accessible_regions[i][-2][2]), encoding="raw_unicode_escape"), #end_pos of the last open state
            blockNum=block_num,
            blockSizes=bytes(str(block_sizes[0:-1]), encoding="raw_unicode_escape"),
            blockStarts=bytes(str(block_starts[0:-1]), encoding="raw_unicode_escape"))

    broadpeak.write_to_gappedPeak(accessible_region_file)
    return

