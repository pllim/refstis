#!/usr/bin/env python

"""Create STIS Superdarks and Superbiases for the CCD detector.

Biases
------
1.  Retrieve new datasets from the archive
2.  Separate darks and biases anneal "weeks"
3.  Run refbias
3a. If fewer than threshold frames, run weekbias
4.  Create new baseline bias file
5.  Update headers.

Darks
-----
1.  Retrieve new datasets from the archive
2.  Create basedark
3.  Create weekly dark using basedark from previous month
4.  Update headers.

Test reference files and deliver.

"""

from __future__ import division

import sqlite3
import glob
import os
import sys
from astropy.io import fits
import argparse
import textwrap
import support
import time
import shutil
import re
import numpy as np

from support import SybaseInterface
from support import createXmlFile, submitXmlFile

from functions import figure_number_of_periods, translate_date_string
import pop_db
import basedark
import weekdark
import refbias
import weekbias
import basejoint
import functions

### products_directory = '/user/ely/STIS/refstis/darks_biases/'
### retrieve_directory = '/user/ely/STIS/refstis/requested/'
products_directory = '/grp/hst/stis/darks_biases/refstis_test/'
retrieve_directory = '/grp/hst/stis/darks_biases/refstis_test/data/'

for location in [products_directory, retrieve_directory]:
    if not os.path.isdir(location):
        os.makedirs(location)


#dark_proposals = [7600, 7601, 8408, 8437, 8837, 8864, 8901, 8902, 9605, 9606,
#                  10017, 10018, 11844, 11845, 12401, 12402, 12741, 12742]
#bias_proposals = [7600, 7601, 8409, 8439, 8838, 8865, 8903, 8904, 9607, 9608,
#                  10019, 10020, 11846, 11847, 12403, 12404, 12743, 12744]

dark_proposals = [11844, 11845, 12400, 12401, 12741, 12742, 13131, 13132, 13518, 13519]
bias_proposals = [11846, 11847, 12402, 12403, 12743, 12744, 13133, 13134, 13535, 13536]


#-------------------------------------------------------------------------------

def get_new_periods():
    print '#-------------------#'
    print 'Reading from database'
    print '#-------------------#\n'
    db = sqlite3.connect( "anneal_info.db" )
    c = db.cursor()
    table = 'anneals'

    c.execute("""SELECT * FROM %s """ % (table))

    all_info = [row for row in c]

    table_id_all = [row[0] for row in all_info]
    proposal_id_all = [row[1] for row in all_info]
    visit_id_all = [int(row[2]) for row in all_info]
    anneal_start_all = [row[3] for row in all_info]
    anneal_end_all = [row[4] for row in all_info]

    dirs_to_process = []

    for i in range( len(table_id_all) )[::-1]:
        if i == len(table_id_all) - 1: continue
        ref_begin = anneal_end_all[i]
        ref_end = anneal_start_all[i + 1]
        #-- defined from proposal of the next anneal
        proposal = proposal_id_all[i + 1]

        visit = visit_id_all[i + 1]
        year, month, day, dec_year = support.mjd_to_greg(ref_begin)
        end_year, end_month, end_day, dec_year = support.mjd_to_greg(ref_end)

        if visit < 10:
            visit = '0' + str(visit)
        else:
            visit = str(visit)

        print '#--------------------------------#'
        print 'Searching for new observations for'
        print 'Period: %d_%d_%s'%(year, proposal, visit)
        print 'MJD %5.5f %5.5f'%(ref_begin, ref_end)
        print month, day, year, ' to ', end_month, end_day, end_year
        print '#--------------------------------#'

        products_folder = os.path.join(products_directory,
                                       '%d_%s' % (proposal, visit))
        dirs_to_process.append(products_folder)

        if not os.path.exists(products_folder):
            os.makedirs(products_folder)
        '''
        already_retrieved = []
        for root, dirs, files in os.walk(products_folder):
            for filename in files:
                if filename.endswith('_raw.fits'):
                    already_retrieved.append( filename[:9].upper() )

        new_obs = get_new_obs('DARK', ref_begin, ref_end) + \
            get_new_obs('BIAS', ref_begin, ref_end)

        obs_to_get = [obs for obs in new_obs if not obs in already_retrieved]

        if not len(obs_to_get):
            print 'No new obs to get, skipping this period\n\n'
            continue
        else:
            print 'Found new observations for this period'
            print obs_to_get, '\n\n'

        ### response = collect_new( obs_to_get )
        ### move_obs( obs_to_get, products_folder)
        separate_obs(products_folder, ref_begin, ref_end)
        '''
    sys.exit('done')
    return dirs_to_process

#-------------------------------------------------------------------------------

def split_files(all_files):
    """Split file list into two smaller lists for iraf tasks

    Each list will have a selection from both early and late in time.

    """

    all_info = [(fits.getval(filename,'EXPSTART', 1), filename)
                for filename in all_files]
    all_info.sort()
    all_files = [line[1] for line in all_info]

    super_list = [all_files[0::2],
                   all_files[1::2]]

    return super_list

#-------------------------------------------------------------------------------

def pull_out_subfolders(root_folder):
    """ Walk through input folder and use regular expressions to return lists of
    the folders for each gain and for each week

    Parameters
    ----------
    root_folder
        string, the folder to walk through

    Returns
    -------
    gain_folders
        list, containing folders for each gain
    week_folders
        list, containing folders for each week

    """

    gain_folders = []
    week_folders = []
    for root, dirs, files in os.walk(root_folder):
        tail = os.path.split(root)[-1]

        if 'wk' in tail:
            week_folders.append(root)

        # ex: 1-1x1 or 4-1x1
        if re.search('([0-4]-[0-4]x[0-4])', tail):
            gain_folders.append(root)

    return gain_folders, week_folders

#-------------------------------------------------------------------------------

def grab_between(file_list, mjd_start, mjd_end):

    for filename in file_list:
        with pyfits.open(filename) as hdu:
            data_start = hdu[0].header['TEXPSTRT']
            data_end = hdu[0].header['TEXPEND']

        if mjd_start < data_start < mjd_end:
            yield filename

#-------------------------------------------------------------------------------

def pull_info(foldername):
    """ Pull proposal and week number from folder name

    A valid proposal is a string of 5 numbers from 0-9
    A valid week is a string of 'wk' + 2 numbers ranging from 0-9.
    A valid biweek is the string 'biwk' + 2 numbers from 0-9.

    Parameters
    ----------
    foldername
        string, name of folder to search in

    Returns
    -------
    proposal
        string, proposal number as string
    week
        string, week of the anneal (wk01, etc)

    """

    try:
        proposal, visit = re.findall('([0-9]{5})_([0-9]{2})', foldername)[0]
    except:
        proposal, visit = '', ''

    try:
        week = re.findall('([bi]*wk0[0-9])', foldername)[0]
    except:
        week = ''

    return proposal, week, visit

#-------------------------------------------------------------------------------

def get_anneal_month(proposal_id, anneal_id):

    db = sqlite3.connect( "anneal_info.db" )
    c = db.cursor()
    table = 'anneals'


    c.execute("""SELECT id,start FROM {} WHERE proposid={} AND visit={}""".format(table,
                                                                                  proposal_id,
                                                                                  anneal_id))

    all_info = [row for row in c]
    if len(all_info) > 1:
        raise ValueError("Too many values returned: {}".format(all_info))
    pri_key, anneal_end = all_info[0]



    c.execute("""SELECT end FROM {} WHERE id={}""".format(table, pri_key - 1))

    all_info = [row for row in c]
    if len(all_info) > 1:
        raise ValueError("Too many values returned: {}".format(all_info))
    anneal_start = all_info[0][0]

    return anneal_start, anneal_end

#-------------------------------------------------------------------------------

def make_pipeline_reffiles(root_folder, last_basedark=None, last_basebias=None):
    """Make reference files like the refstis pipeline

    1.  Separate dark and bias datasets into week folders
    2.

    """

    if not 'oref' in os.environ:
        os.environ['oref'] = '/grp/hst/cdbs/oref/'

    bias_threshold = {(1, 1, 1) : 98,
                      (1, 1, 2) : 25,
                      (1, 2, 1) : 25,
                      (1, 2, 2) : 7,
                      (1, 4, 1) : 7,
                      (1, 4, 2) : 4,
                      (4, 1, 1) : 1}

    print '#-----------------------------#'
    print '#  Making all ref files for   #'
    print  root_folder
    print '#-----------------------------#'

    if not os.path.exists(root_folder):
        raise IOError('Root folder does not exist')

    # separate raw files in folder into periods
    separate_period(root_folder)

    print '###################'
    print ' make the basebias '
    print '###################'
    raw_files = []
    for root, dirs, files in os.walk(os.path.join(root_folder, 'biases')):
        if not '1-1x1' in root:
            continue

        for filename in files:
            if filename.startswith('o') and filename.endswith('_raw.fits'):
                raw_files.append(os.path.join(root, filename))

    basebias_name = os.path.join(root_folder, 'basebias.fits')
    if os.path.exists(basebias_name):
        print '{} already exists, skipping'
    else:
        basejoint.make_basebias(raw_files, basebias_name)

    print '###################'
    print ' make the basedarks '
    print '###################'
    raw_files = []
    for root, dirs, files in os.walk(os.path.join(root_folder, 'darks')):
        for filename in files:
            if filename.startswith('o') and filename.endswith('_raw.fits'):
                raw_files.append(os.path.join(root, filename))

    basedark_name = os.path.join(root_folder, 'basedark.fits')
    if os.path.exists(basedark_name):
        print '{} already exists, skipping'
    else:
        basedark.make_basedark(raw_files, basedark_name, basebias_name)


    print '##################################'
    print ' make the weekly biases and darks '
    print '##################################'
    #-- Find the premade folders if they exist
    gain_folders, week_folders = pull_out_subfolders(root_folder)

    #-- use last basefiles if supplied
    basebias_name = last_basebias or basebias_name
    basedark_name = last_basedark or basedark_name

    for folder in week_folders:
        print 'Processing {}'.format(folder)

        proposal, wk, visit = pull_info(folder)

        raw_files = glob.glob(os.path.join(folder, '*raw.fits'))
        n_imsets = functions.count_imsets(raw_files)

        gain = functions.get_keyword(raw_files, 'CCDGAIN', 0)
        xbin = functions.get_keyword(raw_files, 'BINAXIS1', 0)
        ybin = functions.get_keyword(raw_files, 'BINAXIS2', 0)

        if re.search('/biases/', folder):
            filetype = 'bias'

            weekbias_name = os.path.join(folder,
                                         'weekbias_%s_%s_%s.fits'%(proposal, visit, wk))
            if os.path.exists(weekbias_name):
                print '{} already exists, skipping'
                continue

            #make weekbias if too few imsets
            if n_imsets < bias_threshold[(gain, xbin, ybin)]:
                weekbias.make_weekbias(raw_files, weekbias_name, basebias_name)
            else:
                if n_imsets > 120:
                    super_list = split_files(raw_files)
                    all_subnames = []
                    for i, sub_list in enumerate(super_list):
                        subname = weekbias_name.replace('.fits', '_grp0'+str(i+1)+'.fits')
                        print 'Making sub-file for datasets'
                        print sub_list
                        refbias.make_refbias(sub_list, subname)
                        all_subnames.append(subname)
                    functions.refaver(all_subnames, weekbias_name)
                else:
                    refbias.make_refbias(raw_files, weekbias_name)


        elif re.search('/darks/', folder):
            filetype = 'dark'

            weekdark_name = os.path.join(folder,
                                         'weekdark_%s_%s_%s.fits'%(proposal, visit, wk))
            if os.path.exists(weekdark_name):
                print '{} already exists, skipping'
                continue


            weekbias_name = os.path.join(root_folder,
                                         'biases/1-1x1',
                                          wk,
                                         'weekbias_%s_%s_%s.fits'%(proposal, visit, wk))

            basedark_name = os.path.join(root_folder, 'basedark.fits')
            weekdark.make_weekdark(raw_files,
                                   weekdark_name,
                                   basedark_name,
                                   weekbias_name)

        else:
            raise ValueError("{} doesn't conform with standards".format(folder))

#-------------------------------------------------------------------------------

def make_ref_files(root_folder, clean=False):
    """ Make all refrence files for a given folder

    This functions is very specific to the REFSTIS pipeline, and requires files
    and folders to have certain naming conventions.

    """

    print '#-----------------------------#'
    print '#  Making all ref files for   #'
    print  root_folder
    print '#-----------------------------#'

    if not os.path.exists(root_folder):
        raise IOError('Root folder does not exist')

    if clean:
        clean_directory(root_folder)

    bias_threshold = {(1, 1, 1) : 98,
                      (1, 1, 2) : 25,
                      (1, 2, 1) : 25,
                      (1, 2, 2) : 7,
                      (1, 4, 1) : 7,
                      (1, 4, 2) : 4,
                      (4, 1, 1) : 1}

    #-- Find the premade folders if they exist
    gain_folders, week_folders = pull_out_subfolders(root_folder)

    if not len(gain_folders) or not len(week_folders):
        proposal_id, anneal_id = os.path.split(os.path.realpath(root_folder))[-1].split('_')
        anneal_start, anneal_stop = get_anneal_month(proposal_id, anneal_id)
        print anneal_start, anneal_stop
        datasets = glob.glob(os.path.join(root_folder, '*_raw.fits'))
        separate_obs(root_folder, anneal_start, anneal_stop, datasets)

        gain_folders, week_folders = pull_out_subfolders(root_folder)

    ######################
    # make the base biases
    ######################
    if os.path.exists(os.path.join(root_folder, 'biases')):
        for folder in gain_folders:
            all_dir = os.path.join(folder, 'all')
            if not os.path.exists(all_dir):
                os.mkdir(all_dir)

            for root, dirs, files in os.walk(folder):
                if root.endswith('all'): continue
                for filename in files:
                    if filename.endswith('_raw.fits'):
                        shutil.copy(os.path.join(root, filename), all_dir)

            all_files = glob.glob(os.path.join( all_dir, '*_raw.fits'))
            basebias_name = os.path.join(all_dir, 'basebias.fits')
            if not os.path.exists(basebias_name):
                basejoint.make_basebias(all_files , basebias_name)
            else:
                print 'Basebias already created, skipping'
    else:
        print 'no folder {} exists, not making a basebias'.format(os.path.join(root_folder, 'biases'))

    ######################
    # make the base darks
    ######################
    if os.path.exists(os.path.join(root_folder, 'darks')):
        dark_folder = os.path.join(root_folder, 'darks')
        all_dir = os.path.join(dark_folder, 'all')
        if not os.path.exists(all_dir):  os.mkdir(all_dir)

        for root, dirs, files in os.walk(dark_folder):
            if root.endswith('all'): continue
            for filename in files:
                if filename.endswith('_raw.fits'):
                    shutil.copy( os.path.join(root, filename), all_dir)

        all_files = glob.glob(os.path.join(all_dir, '*_raw.fits'))

        basebias_name = os.path.join(root_folder,
                                     'biases/1-1x1/all/',
                                     'basebias.fits' )
        basedark_name = os.path.join(all_dir, 'basedark.fits')
        if not os.path.exists(basedark_name):
            basedark.make_basedark(all_files , basedark_name, basebias_name)
        else:
            print 'Basedark already created, skipping'
    else:
        print 'no folder {} exists, not making a basedark'.format(os.path.join(root_folder, 'darks'))

    ####################
    # make the weekly biases and darks
    ####################
    for folder in week_folders:
        REFBIAS = False
        WEEKBIAS = False

        BASEDARK = False
        WEEKDARK = False
        print 'Processing %s'%(folder)

        proposal, wk, visit = pull_info(folder)

        raw_files = glob.glob(os.path.join(folder, '*raw.fits'))
        n_imsets = functions.count_imsets(raw_files)

        gain = functions.get_keyword(raw_files, 'CCDGAIN', 0)
        xbin = functions.get_keyword(raw_files, 'BINAXIS1', 0)
        ybin = functions.get_keyword(raw_files, 'BINAXIS2', 0)

        if re.search('/biases/', folder):
            filetype = 'bias'
            REFBIAS = True

            ### What does this mean?
            if n_imsets < bias_threshold[(gain, xbin, ybin)]:
                WEEKBIAS = True

        elif re.search('/darks/', folder):
            filetype = 'dark'
            BASEDARK = True
            WEEKDARK = True

        else:
            raise ValueError("{} doesn't conform with standards".format(folder))


        print 'Making REFFILE for ', filetype
        print '%d files found with %d imsets'%(len(raw_files), n_imsets)

        if REFBIAS:
            refbias_name = os.path.join(folder,
                                        'refbias_%s_%s.fits'%(proposal, wk))
            if os.path.exists(refbias_name):
                print 'Refbias already created, skipping'
            else:
                if n_imsets > 120:
                    super_list = split_files(raw_files)
                    all_subnames = []
                    for i, sub_list in enumerate(super_list):
                        subname = refbias_name.replace('.fits',
                                                       '_grp0'+str(i+1)+'.fits')
                        print 'Making sub-file for datasets'
                        print sub_list
                        refbias.make_refbias(sub_list, subname)
                        all_subnames.append(subname)
                    functions.refaver(all_subnames, refbias_name)
                else:
                    refbias.make_refbias(raw_files, refbias_name)

        if WEEKBIAS:
            weekbias_name = os.path.join(folder,
                                         'weekbias_%s_%s.fits'%(proposal, wk))
            if os.path.exists(weekbias_name):
                print 'Weekbias already created, skipping'
            else:
                refbias.make_refbias(raw_files, weekbias_name, basebias_name)

        if WEEKDARK:
            weekdark_name = os.path.join(folder,
                                         'weekdark_%s_%s.fits'%(proposal, wk))
            if os.path.exists( weekdark_name ):
                print 'Weekdark already created, skipping'
            else:
                ### probably need to be final file, either week* or ref*
                weekbias_name = os.path.join(root_folder,
                                             'biases/1-1x1',
                                             wk,
                                             'refbias_%s_%s.fits'%(proposal, wk))
                basedark_name = os.path.join(folder.replace(wk, 'all'),
                                             'basedark.fits')
                weekdark.make_weekdark(raw_files,
                                       weekdark_name,
                                       basedark_name,
                                       weekbias_name )

#-------------------------------------------------------------------------------

def clean_directory(root_path):
    """ Cleans directory of any fits files that do not end in _raw.fits

    This WILL remove ANY files that do not match *_raw.fits.  This includes
    any plots, txt files, other fits files, anything.

    Use with caution

    """

    for root, dirs, files in os.walk(root_path):
        for filename in files:
            if not filename.endswith('_raw.fits'):
                print 'Removing: ', filename
                os.remove(os.path.join( root, filename))

#-------------------------------------------------------------------------------

def get_new_obs(file_type, start, end):

    if file_type == 'DARK':
        proposal_list = dark_proposals
        MIN_EXPTIME = 1000
        MAX_EXPTIME = 1200
    elif file_type == 'BIAS':
        proposal_list = bias_proposals
        MIN_EXPTIME = -1
        MAX_EXPTIME = 100
    else:
        print 'file type not recognized: ', file_type

    query = support.SybaseInterface("ZEPPO", "dadsops")

    OR_part = "".join(["science.sci_pep_id = %d OR "%(proposal) for proposal in proposal_list])[:-3]

    data_query = "SELECT science.sci_start_time,science.sci_data_set_name FROM science WHERE ( " + OR_part + " ) AND  science.sci_targname ='%s' AND science.sci_actual_duration BETWEEN %d AND %d "%(file_type, MIN_EXPTIME, MAX_EXPTIME)
    query.doQuery(query=data_query)
    new_dict = query.resultAsDict()

    obs_names = np.array( new_dict['sci_data_set_name'] )

    start_times_MJD = np.array( map(translate_date_string, new_dict['sci_start_time'] ) )

    index = np.where( (start_times_MJD > start) & (start_times_MJD < end) )[0]

    if not len( index ):
        print "WARNING: didn't find any datasets, skipping"
        return []

    assert start_times_MJD[index].min() > start, 'Data has mjd before period start'
    assert start_times_MJD[index].max() < end, 'Data has mjd after period end'

    datasets_to_retrieve = obs_names[index]

    return list(datasets_to_retrieve)

#-----------------------------------------------------------------------

def collect_new(observations_to_get):
    '''
    Function to find and retrieve new datasets for given proposal.
    Uses modules created by B. York: DADSAll.py and SybaseInterface.py.
    '''

    xml = createXmlFile(ftp_dir=retrieve_directory,
                        set=observations_to_get,
                        file_type='RAW')

    response = submitXmlFile(xml, 'dmsops1.stsci.edu')
    if ('SUCCESS' in response):
        return True
    else:
        return False

#-----------------------------------------------------------------------

def separate_period(base_dir):
    """Separate observations in the base dir into needed folders.

    Parameters
    ----------
    base_dir, str
        directory containing darks and biases to be split.

    """


    print 'Separating', base_dir
    all_files = glob.glob(os.path.join(base_dir, 'o*_raw.fits'))
    if not len(all_files):
        print "nothing to move"
        return

    mjd_times = np.array([fits.getval(item, 'EXPSTART', ext=1)
                          for item in all_files])
    month_begin = mjd_times.min()
    month_end = mjd_times.max()
    print 'All data goes from', month_begin, ' to ',  month_end

    select_gain = {'WK' : 1,
                   'BIWK' : 4}

    for file_type, mode in zip(['BIAS', 'DARK', 'BIAS'],
                               ['WK', 'WK', 'BIWK']):

        gain = select_gain[mode]

        obs_list = []
        for item in all_files:
            with fits.open(item) as hdu:
                if (hdu[0].header['TARGNAME'] == file_type) and (hdu[0].header['CCDGAIN'] == gain):
                    obs_list.append(item)

        if not len(obs_list):
            print '{} No obs to move.  Skipping'.format(mode)
            continue
        else:
            print file_type,  mode, len(obs_list), 'files to move, ', 'gain = ', gain

        N_days = int(month_end - month_begin)
        N_periods = figure_number_of_periods(N_days, mode)
        week_lengths = functions.figure_days_in_period(N_periods, N_days)

        #--Add remainder to end
        week_lengths[-1] += (month_end - month_begin) - N_days

        #-- Translate to MJD
        anneal_weeks = []
        start = month_begin
        end = start + week_lengths[0]
        anneal_weeks.append((start, end))
        for item in week_lengths[1:]:
            start = end
            end += item
            anneal_weeks.append((start, end))

        print
        print file_type, mode, 'will be broken up into %d periods as follows:'%(N_periods)
        print '\tWeek start, Week end'
        for a_week in anneal_weeks:
            print '\t', a_week
        print

        for period in xrange(N_periods):
            begin, end = anneal_weeks[period]
            # weeks from 1-4, not 0-3
            week = str(period + 1)
            while len(week) < 2:
                week = '0' + week

            output_path = base_dir
            if file_type == 'BIAS':
                output_path = os.path.join(output_path,
                                           'biases/%d-1x1/%s%s/'%(gain,
                                                                  mode.lower(),
                                                                  week))
            elif file_type == 'DARK':
                output_path = os.path.join(output_path,
                                           'darks/%s%s/'%(mode.lower(), week))
            else:
                print 'File Type not recognized'

            print output_path
            if not os.path.exists(output_path):
                os.makedirs(output_path)

            print 'week goes from: ', begin, end
            obs_to_move = [item for item in obs_list if
                           (begin <= fits.getval(item, 'EXPSTART', ext=1) <= end)]

            if not len(obs_to_move):
                raise ValueError('error, empty list to move')

            for item in obs_to_move:
                print 'Moving ', item,  ' to:', output_path
                shutil.move(item,  output_path)
                if not 'IMPHTTAB' in fits.getheader(os.path.join(output_path,
                                                                 item.split('/')[-1]), 0):
                    ###Dynamic at some point
                    fits.setval(os.path.join(output_path, item.split('/')[-1]),
                                'IMPHTTAB',
                                ext=0,
                                value='oref$x9r1607mo_imp.fits')

                obs_list.remove(item)
                all_files.remove(item)

#-----------------------------------------------------------------------


def separate_obs(base_dir, month_begin, month_end, all_files=None):
    if not all_files:
        all_files = glob.glob(os.path.join(retrieve_directory, '*raw.fits'))

    print 'Separating', base_dir
    print
    print 'Period runs from', month_begin, ' to ',  month_end

    mjd_times = np.array([fits.getval(item, 'EXPSTART', ext=1)
                          for item in all_files])
    print 'All data goes from', mjd_times.min(), ' to ',  mjd_times.max()

    select_gain = {'WK' : 1,
                   'BIWK' : 4}

    for file_type, mode in zip(['BIAS', 'DARK', 'BIAS'],
                               ['WK', 'WK', 'BIWK']):

        gain = select_gain[mode]

        obs_list = []
        for item in all_files:
            with fits.open(item) as hdu:
                if (hdu[0].header['TARGNAME'] == file_type) and (hdu[0].header['CCDGAIN'] == gain):
                    obs_list.append(item)

        if not len(obs_list):
            print '%s No obs to move.  Skipping'%(mode)
            continue

        print file_type,  mode, len(obs_list), 'files to move, ', 'gain = ', gain

        N_days = int(round(month_end - month_begin))
        N_periods = figure_number_of_periods(N_days, mode)
        week_lengths = functions.figure_days_in_period(N_periods, N_days)

        anneal_weeks = [(month_begin + item - week_lengths[0], month_begin + item) for item in np.cumsum(week_lengths)]

        print
        print file_type, mode, 'will be broken up into %d periods as follows:'%(N_periods)
        print '\tWeek start, Week end'
        for a_week in anneal_weeks:
            print '\t', a_week
        print

        for period in xrange(N_periods):
            begin, end = anneal_weeks[period]
            # weeks from 1-4, not 0-3
            week = str(period + 1)
            while len(week) < 2:
                week = '0'+week

            output_path = base_dir
            if file_type == 'BIAS':
                output_path = os.path.join(output_path,
                                           'biases/%d-1x1/%s%s/'%(gain,
                                                                  mode.lower(),
                                                                  week))
            elif file_type == 'DARK':
                output_path = os.path.join(output_path,
                                           'darks/%s%s/'%(mode.lower(), week))
            else:
                print 'File Type not recognized'

            print output_path
            if not os.path.exists(output_path):
                os.makedirs(output_path)

            print 'week goes from: ', begin, end
            obs_to_move = [item for item in obs_list if
                            ((fits.getval(item, 'EXPSTART', ext=1) >= begin) and
                             (fits.getval(item, 'EXPSTART', ext=1) < end))]

            if not len(obs_to_move):
                print 'error, empty list to move'

            for item in obs_to_move:
                print 'Moving ', item,  ' to:', output_path
                shutil.move(item,  output_path)
                if not 'IMPHTTAB' in fits.getheader(os.path.join(output_path,
                                                                 item.split('/')[-1]), 0):
                    ###Dynamic at some point
                    fits.setval(os.path.join(output_path, item.split('/')[-1]),
                                'IMPHTTAB', ext = 0, value = 'oref$x9r1607mo_imp.fits')
                obs_list.remove(item)
                all_files.remove(item)

#-----------------------------------------------------------------------

def move_obs(new_obs, base_output_dir):
    print 'Files not yet delivered.'
    delivered_set = set( [ os.path.split(item)[-1][:9].upper() for
                           item in glob.glob( os.path.join(retrieve_directory, '*raw*.fits') ) ] )
    new_set = set(new_obs)

    while not new_set.issubset(delivered_set):
        wait_minutes = 2
        time.sleep(wait_minutes * 60) #sleep for 2 min
        delivered_set = set([ os.path.split(item)[-1][:9].upper() for
                              item in glob.glob( os.path.join(retrieve_directory, '*raw*.fits') ) ])

    assert len(new_obs) > 0, 'Empty list of new observations to move.'

    if not os.path.exists( base_output_dir):
        os.makedirs( base_output_dir )

    list_to_move = [ os.path.join( retrieve_directory, item.lower()+'_raw.fits') for item in new_obs ]

    for item in list_to_move:
        print 'Moving ', item,  ' to:', base_output_dir
        shutil.move( item, base_output_dir )

    list_to_remove = glob.glob( os.path.join(retrieve_directory, '*.fits') )
    for item in list_to_remove:
        os.remove(item)

#-------------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=textwrap.dedent('''\
Description
------------------------------------
  Automated script for producing
  dark and bias reference files for
  STIS CCD data.

Locations
------------------------------------
  Product directory = EMPTY

Procedure
------------------------------------
1: pass

2: pass

------------------------------------
''' ) )

    parser.add_argument("-r",
                        "--redo_all",
                        action='store_true',
                        dest="redo_all",
                        default=False,
                        help="Re-run analysis on all past anneal months.")
    parser.add_argument("-c",
                        "--no_collect",
                        action='store_false',
                        dest="collect_new",
                        default=True,
                        help="Turn off data collection function.")
    parser.add_argument("-p",
                        "--plots_only",
                        action='store_true',
                        dest="plots_only",
                        default=False,
                        help="Only remake plots and update the website.")
    parser.add_argument("-u",
                        "--user_information",
                        action='store',
                        dest="user_information",
                        default=None,
                        help="info string needed to request data")

    return parser.parse_args()

#-----------------------------------------------------------------------

def run():
    """Run the reference file pipeline """

    args = parse_args()

    pop_db.main()

    all_folders = get_new_periods()

    for folder in all_folders:
        make_ref_files(folder, clean=args.redo_all)

#-----------------------------------------------------------------------

if __name__ == "__main__":
    run()
