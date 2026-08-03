# openSEPPO to search NISAR Data and process GCOV Products

Author: Josef Kellndorfer

This example shows the use of openSEPPO tools to search and convert NISAR stacks to COGs, GTiff or simply subset to h5.

For full documentation see https://openseppo.readthedocs.io


```python
import os
from  openseppo.cli import nisar_search, nisar_gcov_convert
```

# 1. Data Search 
## Search available data at a point


```python
LON = -71
LAT = 46
start_time_after = "2026-06-17"
cmd = f'seppo_nisar_search --point {LON} {LAT} --start_time_after {start_time_after} --group --https'
print(cmd)
```

    seppo_nisar_search --point -71 46 --start_time_after 2026-06-17 --group --https



```python
nisar_search._main(cmd)
```

    === Track: 003 | Direction: A | Frame: 025 | Product: GCOV ===
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_024_003_A_025_4005_DHDH_A_20260626T093524_20260626T093559_P05023_N_F_J_001/NISAR_L2_PR_GCOV_024_003_A_025_4005_DHDH_A_20260626T093524_20260626T093559_P05023_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_025_003_A_025_4005_SHSH_A_20260708T093523_20260708T093558_P05023_N_F_J_001/NISAR_L2_PR_GCOV_025_003_A_025_4005_SHSH_A_20260708T093523_20260708T093558_P05023_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_026_003_A_025_4005_DHDH_A_20260720T093522_20260720T093557_P05023_N_F_J_001/NISAR_L2_PR_GCOV_026_003_A_025_4005_DHDH_A_20260720T093522_20260720T093557_P05023_N_F_J_001.h5
    
    === Track: 069 | Direction: D | Frame: 065 | Product: GCOV ===
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_023_069_D_065_4005_DHDH_A_20260618T235045_20260618T235120_P05023_N_F_J_001/NISAR_L2_PR_GCOV_023_069_D_065_4005_DHDH_A_20260618T235045_20260618T235120_P05023_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_024_069_D_065_4005_DHDH_A_20260630T235044_20260630T235119_P05023_N_F_J_001/NISAR_L2_PR_GCOV_024_069_D_065_4005_DHDH_A_20260630T235044_20260630T235119_P05023_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_025_069_D_065_4005_DHDH_A_20260712T235043_20260712T235119_P05023_N_F_J_001/NISAR_L2_PR_GCOV_025_069_D_065_4005_DHDH_A_20260712T235043_20260712T235119_P05023_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_026_069_D_065_4005_DHDH_A_20260724T235043_20260724T235118_P05023_N_F_J_001/NISAR_L2_PR_GCOV_026_069_D_065_4005_DHDH_A_20260724T235043_20260724T235118_P05023_N_F_J_001.h5
    
    === Track: 104 | Direction: A | Frame: 025 | Product: GCOV ===
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T094344_20260621T094419_P05023_N_F_J_001/NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T094344_20260621T094419_P05023_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_024_104_A_025_4005_DHDH_A_20260703T094343_20260703T094418_P05023_N_F_J_001/NISAR_L2_PR_GCOV_024_104_A_025_4005_DHDH_A_20260703T094343_20260703T094418_P05023_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_025_104_A_025_4005_DHDH_A_20260715T094342_20260715T094417_P05023_N_F_J_001/NISAR_L2_PR_GCOV_025_104_A_025_4005_DHDH_A_20260715T094342_20260715T094417_P05023_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_026_104_A_025_4005_DHDH_A_20260727T094342_20260727T094417_P05023_N_F_J_001/NISAR_L2_PR_GCOV_026_104_A_025_4005_DHDH_A_20260727T094342_20260727T094417_P05023_N_F_J_001.h5


## Let's pick track 104 frame 25 and generate a url list output


```python
track = 104
frame = 25
out = f"{os.environ['HOME']}/search_result_httpurls.txt"
start_time_after = "2026-06-17"
cmd = f"seppo_nisar_search --track {track} --frame {frame} --start_time_after {start_time_after} --https -o {out}"
print(cmd)
```

    seppo_nisar_search --track 104 --frame 25 --start_time_after 2026-06-17 --https -o /home/josefk/search_result_httpurls.txt



```python
nisar_search._main(cmd)
```


```python
! cat $HOME/search_result_httpurls.txt
```

    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T094344_20260621T094419_P05023_N_F_J_001/NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T094344_20260621T094419_P05023_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_024_104_A_025_4005_DHDH_A_20260703T094343_20260703T094418_P05023_N_F_J_001/NISAR_L2_PR_GCOV_024_104_A_025_4005_DHDH_A_20260703T094343_20260703T094418_P05023_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_025_104_A_025_4005_DHDH_A_20260715T094342_20260715T094417_P05023_N_F_J_001/NISAR_L2_PR_GCOV_025_104_A_025_4005_DHDH_A_20260715T094342_20260715T094417_P05023_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_026_104_A_025_4005_DHDH_A_20260727T094342_20260727T094417_P05023_N_F_J_001/NISAR_L2_PR_GCOV_026_104_A_025_4005_DHDH_A_20260727T094342_20260727T094417_P05023_N_F_J_001.h5


# 2. Data Inspection
## Let's inspect the first link and available VARS 

The `-lg|--list_grids` picks the first url if a list is provided with the `-i <URL_LIST` flag. If you want to inspect a specific url, you can provide the url directly to the `-i <URL>` flag


```python
cmd = f"seppo_nisar_gcov_convert -lg -i {out}"
print(cmd)
```

    seppo_nisar_gcov_convert -lg -i /home/josefk/search_result_httpurls.txt



```python
nisar_gcov_convert._main(cmd)
```

    ---> Detected Earthdata HTTPS URL. Using Earthdata credentials.
    Starting Batch Processing: 4 files.
    Mode: pwr | Freq: A | Downscale: None
    Inspecting file: https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_PROVISIONAL_V1/NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T094344_20260621T094419_P05023_N_F_J_001/NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T094344_20260621T094419_P05023_N_F_J_001.h5



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


    
    Available Grids in HDF5:
      Frequency A:
        CRS: EPSG:32619
        Raster Size:  35640 x 35208 pixels (cols/rows)
        Resolution: X=10.00, Y=-10.00
        Extent (W,S,E,N): [76320.00, 4970160.00, 432720.00, 5322240.00]
        Footprint (Lon/Lat): (-74.2823, 47.0811), (-71.1053, 47.8984), (-70.0482, 45.8217), (-73.1220, 45.0336)
        Footprint (Native):  (99057.59, 5227732.00), (342646.84, 5307152.23), (418576.39, 5074767.60), (175315.84, 4994950.58)
        Frame Size:          Width: 256.11 km, Height: 244.71 km
        Variables:
          HHHH                            dtype=float32  nodata=nan
          HVHV                            dtype=float32  nodata=nan
          inputDataExceptionMask          dtype=uint8  nodata=none
          mask                            dtype=uint8  nodata=255
          numberOfLooks                   dtype=float32  nodata=nan
          rtcGammaToSigmaFactor           dtype=float32  nodata=nan
      Frequency B:
        CRS: EPSG:32619
        Raster Size:  4455 x 4401 pixels (cols/rows)
        Resolution: X=80.00, Y=-80.00
        Extent (W,S,E,N): [76320.00, 4970160.00, 432720.00, 5322240.00]
        Footprint (Lon/Lat): (-74.2823, 47.0811), (-71.1053, 47.8984), (-70.0482, 45.8217), (-73.1220, 45.0336)
        Footprint (Native):  (99057.59, 5227732.00), (342646.84, 5307152.23), (418576.39, 5074767.60), (175315.84, 4994950.58)
        Frame Size:          Width: 256.11 km, Height: 244.71 km
        Variables:
          HHHH                            dtype=float32  nodata=nan
          HVHV                            dtype=float32  nodata=nan
          inputDataExceptionMask          dtype=uint8  nodata=none
          mask                            dtype=uint8  nodata=255
          numberOfLooks                   dtype=float32  nodata=nan
          rtcGammaToSigmaFactor           dtype=float32  nodata=nan
    
    Inspection Complete.


# 3. Data Processing
## Lets pick a small subset to generate the time series for 


We can do this 
- with `--srcwin <XOFF> <YOFF> <XSIZE> <YSIZE>`
- with `--projwin <ULX} <ULY> <LRX> <LRY>` in the native EPSG Coordinates
- with `--projwin` in Lon/Lat coordinates using `-t_srs 4326`. Optionally also set the target resolution e.g. `-tr 0.0002 0.0002`

We are interested in scaling the output to amplitude (`-amp` flag).

We also want to output the data on our s3:// bucket for direct streaming into QGIS later

### Example lon/lat subset to local disk


```python
projwin = "-72 46 -71.8 45.8"
tr= "0.0002 0.0002"
t_srs = 4326
scaling = "-amp"
verbose = "-v"
# Local output
output=f"{os.environ["HOME"]}/openSEPPO_testoutput{scaling}"
# S3 output
output=f"s3://seppo1-data/NISAR/openSEPPO_testoutput{scaling}"
cmd = f"seppo_nisar_gcov_convert -i {out} --projwin {projwin} -t_srs {t_srs} -tr {tr} -o {output} {scaling} {verbose}"
print(cmd)
```

    seppo_nisar_gcov_convert -i /home/josefk/search_result_httpurls.txt --projwin -72 46 -71.8 45.8 -t_srs 4326 -tr 0.0002 0.0002 -o s3://seppo1-data/NISAR/openSEPPO_testoutput-amp -amp -v



```python
nisar_gcov_convert._main(cmd)
```

    {'all_freq': False,
     'cache': None,
     'downscale': None,
     'dualpol_ratio': False,
     'fill_holes': False,
     'freq': None,
     'h5': ['/home/josefk/search_result_httpurls.txt'],
     'input_profile': None,
     'keep_cached': False,
     'list_grids': False,
     'mode': 'AMP',
     'no_tap': False,
     'no_time_series': False,
     'no_vrt': False,
     'nomask': False,
     'output': 's3://seppo1-data/NISAR/openSEPPO_testoutput-amp',
     'output_format': 'COG',
     'output_profile': None,
     'profile': None,
     'projwin': [-72.0, 46.0, -71.8, 45.8],
     'projwin_srs': None,
     'read_threads': 8,
     'rebuild_only': False,
     'resample': 'cubic',
     'reset_vrts': False,
     'show_vrts': False,
     'sigma0': False,
     'single_bands': True,
     'srcwin': None,
     'target_res': [0.0002, 0.0002],
     'target_srs': '4326',
     'use_earthdata': False,
     'vars': None,
     'verbose': True,
     'vsis3': False,
     'warp_threads': None}


    ---> Detected Earthdata HTTPS URL. Using Earthdata credentials.
    Starting Batch Processing: 4 files.
    Mode: AMP | Freq: A | Downscale: None
        [t] earthaccess login (cached token, expires 2026-09-15): instant


    Batch Processing Started: 4 files.
    No variables specified. Auto-detecting Covariance variables for Frequency A...



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


      -> Selected: ['HHHH', 'HVHV']
    --> Processing File: NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T094344_20260621T094419_P05023_N_F_J_001.h5



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] file open + metadata: 1.5s


        Date: 2026-06-21 | Grid: 10.0m (A) | Mode: h5py


        Reprojecting: EPSG:32619 -> 4326 (resample=cubic)


        Reprojection: expanded native projwin [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155]


        Slice (Map native): [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155] -> Pixels: 19052,22378,1638,2285


        Extracting 3 bands...



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        Masking: 0 fill/invalid px (0.0%) set to nodata on backscatter


        [t] data read (2x2285x1638, 29.9 MB): 6.2s


        Using explicit target resolution: 0.0002 x 0.0002


        Reprojecting 2 backscatter + 0 ancillary bands (~0.0 GB)...


        Transforming: AMP (Mode: amp)


        Writing separate bands...


        [t] COG write (2 bands, 0.0 MB): 0.2s


        Generated Snapshot VRT: s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T094344_20260621T094419_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt


        [OK] Complete (standard (all-bands) mode)


    --> Processing File: NISAR_L2_PR_GCOV_024_104_A_025_4005_DHDH_A_20260703T094343_20260703T094418_P05023_N_F_J_001.h5



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] file open + metadata: 1.4s


        Date: 2026-07-03 | Grid: 10.0m (A) | Mode: h5py


        Reprojecting: EPSG:32619 -> 4326 (resample=cubic)


        Reprojection: expanded native projwin [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155]


        Slice (Map native): [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155] -> Pixels: 19052,22378,1638,2285


        Extracting 3 bands...



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        Masking: 0 fill/invalid px (0.0%) set to nodata on backscatter


        [t] data read (2x2285x1638, 29.9 MB): 6.4s


        Using explicit target resolution: 0.0002 x 0.0002


        Reprojecting 2 backscatter + 0 ancillary bands (~0.0 GB)...


        Transforming: AMP (Mode: amp)


        Writing separate bands...


        [t] COG write (2 bands, 0.0 MB): 0.2s


        Generated Snapshot VRT: s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_024_104_A_025_4005_DHDH_A_20260703T094343_20260703T094418_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt


        [OK] Complete (standard (all-bands) mode)


    --> Processing File: NISAR_L2_PR_GCOV_025_104_A_025_4005_DHDH_A_20260715T094342_20260715T094417_P05023_N_F_J_001.h5



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] file open + metadata: 1.3s


        Date: 2026-07-15 | Grid: 10.0m (A) | Mode: h5py


        Reprojecting: EPSG:32619 -> 4326 (resample=cubic)


        Reprojection: expanded native projwin [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155]


        Slice (Map native): [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155] -> Pixels: 19052,22378,1638,2285


        Extracting 3 bands...



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        Masking: 0 fill/invalid px (0.0%) set to nodata on backscatter


        [t] data read (2x2285x1638, 29.9 MB): 5.7s


        Using explicit target resolution: 0.0002 x 0.0002


        Reprojecting 2 backscatter + 0 ancillary bands (~0.0 GB)...


        Transforming: AMP (Mode: amp)


        Writing separate bands...


        [t] COG write (2 bands, 0.0 MB): 0.2s


        Generated Snapshot VRT: s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_025_104_A_025_4005_DHDH_A_20260715T094342_20260715T094417_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt


        [OK] Complete (standard (all-bands) mode)


    --> Processing File: NISAR_L2_PR_GCOV_026_104_A_025_4005_DHDH_A_20260727T094342_20260727T094417_P05023_N_F_J_001.h5



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] file open + metadata: 1.2s


        Date: 2026-07-27 | Grid: 10.0m (A) | Mode: h5py


        Reprojecting: EPSG:32619 -> 4326 (resample=cubic)


        Reprojection: expanded native projwin [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155]


        Slice (Map native): [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155] -> Pixels: 19052,22378,1638,2285


        Extracting 3 bands...



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        Masking: 0 fill/invalid px (0.0%) set to nodata on backscatter


        [t] data read (2x2285x1638, 29.9 MB): 6.9s


        Using explicit target resolution: 0.0002 x 0.0002


        Reprojecting 2 backscatter + 0 ancillary bands (~0.0 GB)...


        Transforming: AMP (Mode: amp)


        Writing separate bands...


        [t] COG write (2 bands, 0.0 MB): 0.2s


        Generated Snapshot VRT: s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_026_104_A_025_4005_DHDH_A_20260727T094342_20260727T094417_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt


        [OK] Complete (standard (all-bands) mode)


    Generating Time Series VRTs...
      --> VRT: NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T000000_20260727T235959_P05023_N_F_J_001-EBD_A_hh_AMP.vrt
      --> VRT: NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T000000_20260727T235959_P05023_N_F_J_001-EBD_A_hv_AMP.vrt
    
    Batch Complete. Generated 2 Time Series VRTs.
    
    Building per-track time series VRTs...
      build_track_vrts: 8 backscatter + 0 ancillary TIFs across 1 track(s).
        TS VRT: NISAR_L2_PR_GCOV_023-026_104_A_025_4005_DHDH_A_20260621T094344_20260727T094417-EBD_A_hh_AMP.vrt
        TS VRT: NISAR_L2_PR_GCOV_023-026_104_A_025_4005_DHDH_A_20260621T094344_20260727T094417-EBD_A_hv_AMP.vrt
        Syncing VRTs to s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/...


    upload: ../../../../../tmp/openseppo_vrts__jcrj9se/NISAR_L2_PR_GCOV_023-026_104_A_025_4005_DHDH_A_20260621T094344_20260727T094417-EBD_A_hh_AMP.vrt to s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_023-026_104_A_025_4005_DHDH_A_20260621T094344_20260727T094417-EBD_A_hh_AMP.vrt
    upload: ../../../../../tmp/openseppo_vrts__jcrj9se/NISAR_L2_PR_GCOV_025_104_A_025_4005_DHDH_A_20260715T094342_20260715T094417_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt to s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_025_104_A_025_4005_DHDH_A_20260715T094342_20260715T094417_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt
    upload: ../../../../../tmp/openseppo_vrts__jcrj9se/NISAR_L2_PR_GCOV_024_104_A_025_4005_DHDH_A_20260703T094343_20260703T094418_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt to s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_024_104_A_025_4005_DHDH_A_20260703T094343_20260703T094418_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt
    upload: ../../../../../tmp/openseppo_vrts__jcrj9se/NISAR_L2_PR_GCOV_023-026_104_A_025_4005_DHDH_A_20260621T094344_20260727T094417-EBD_A_hv_AMP.vrt to s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_023-026_104_A_025_4005_DHDH_A_20260621T094344_20260727T094417-EBD_A_hv_AMP.vrt
    upload: ../../../../../tmp/openseppo_vrts__jcrj9se/NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T094344_20260621T094419_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt to s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T094344_20260621T094419_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt
    upload: ../../../../../tmp/openseppo_vrts__jcrj9se/NISAR_L2_PR_GCOV_026_104_A_025_4005_DHDH_A_20260727T094342_20260727T094417_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt to s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_026_104_A_025_4005_DHDH_A_20260727T094342_20260727T094417_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt
    
    ---> Backscatter:
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_023_104_A_025_4005_DHDH_A_20260621T094344_20260621T094419_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_024_104_A_025_4005_DHDH_A_20260703T094343_20260703T094418_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_025_104_A_025_4005_DHDH_A_20260715T094342_20260715T094417_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_026_104_A_025_4005_DHDH_A_20260727T094342_20260727T094417_P05023_N_F_J_001-EBD_A_hhhv_AMP.vrt
    
    ---> Backscatter time series by track:
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_023-026_104_A_025_4005_DHDH_A_20260621T094344_20260727T094417-EBD_A_hh_AMP.vrt
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_023-026_104_A_025_4005_DHDH_A_20260621T094344_20260727T094417-EBD_A_hv_AMP.vrt
    
    ---> Bucket:
    seppo1-data
    
    Runtime: 0m 36.94s
    


# Display COGs in QGIS

To Display the converted data in QGIS simply choose the `Protocol AWS s3` option, fill in bucket name and one of the VRTs object path from the final output. If you are interested in a simple Timeseries interactive click/plot tool, install from zip our Timeseries SAR plugin from https://github.com/EarthBigData/openSAR/tree/master/code/QGIS/v3/plugins


```python

```
