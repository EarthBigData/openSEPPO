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
cmd = f'seppo_nisar_search --point {LON} {LAT} --group --https'
print(cmd)
```

    seppo_nisar_search --point -71 46 --group --https



```python
nisar_search._main(cmd)
```

    === Track: 003 | Direction: A | Frame: 025 ===
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_010_003_A_025_4005_DHDH_A_20260109T093537_20260109T093600_X05010_N_P_J_001/NISAR_L2_PR_GCOV_010_003_A_025_4005_DHDH_A_20260109T093537_20260109T093600_X05010_N_P_J_001.h5
    
    === Track: 069 | Direction: D | Frame: 065 ===
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_003_069_D_065_4005_DHDH_A_20251021T235042_20251021T235117_X05009_N_F_J_001/NISAR_L2_PR_GCOV_003_069_D_065_4005_DHDH_A_20251021T235042_20251021T235117_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_004_069_D_065_4005_DHDH_A_20251102T235042_20251102T235117_X05010_N_F_J_001/NISAR_L2_PR_GCOV_004_069_D_065_4005_DHDH_A_20251102T235042_20251102T235117_X05010_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_005_069_D_065_4005_DHDH_A_20251114T235043_20251114T235118_X05009_N_F_J_001/NISAR_L2_PR_GCOV_005_069_D_065_4005_DHDH_A_20251114T235043_20251114T235118_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_006_069_D_065_4005_DHDH_A_20251126T235043_20251126T235118_X05009_N_F_J_001/NISAR_L2_PR_GCOV_006_069_D_065_4005_DHDH_A_20251126T235043_20251126T235118_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_007_069_D_065_4005_DHDH_A_20251208T235044_20251208T235119_X05009_N_F_J_001/NISAR_L2_PR_GCOV_007_069_D_065_4005_DHDH_A_20251208T235044_20251208T235119_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_008_069_D_065_4005_DHDH_A_20251220T235044_20251220T235119_X05009_N_F_J_001/NISAR_L2_PR_GCOV_008_069_D_065_4005_DHDH_A_20251220T235044_20251220T235119_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_009_069_D_065_4005_DHDH_A_20260101T235045_20260101T235120_X05009_N_F_J_001/NISAR_L2_PR_GCOV_009_069_D_065_4005_DHDH_A_20260101T235045_20260101T235120_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_010_069_D_065_4005_DHDH_A_20260113T235045_20260113T235121_X05010_N_F_J_001/NISAR_L2_PR_GCOV_010_069_D_065_4005_DHDH_A_20260113T235045_20260113T235121_X05010_N_F_J_001.h5
    
    === Track: 104 | Direction: A | Frame: 025 ===
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_004_104_A_025_4005_DHDH_A_20251105T094341_20251105T094416_X05009_N_F_J_001/NISAR_L2_PR_GCOV_004_104_A_025_4005_DHDH_A_20251105T094341_20251105T094416_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_005_104_A_025_4005_DHDH_A_20251117T094342_20251117T094417_X05009_N_F_J_001/NISAR_L2_PR_GCOV_005_104_A_025_4005_DHDH_A_20251117T094342_20251117T094417_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_006_104_A_025_4005_DHDH_A_20251129T094342_20251129T094417_X05009_N_F_J_001/NISAR_L2_PR_GCOV_006_104_A_025_4005_DHDH_A_20251129T094342_20251129T094417_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_007_104_A_025_4005_DHDH_A_20251211T094343_20251211T094418_X05009_N_F_J_001/NISAR_L2_PR_GCOV_007_104_A_025_4005_DHDH_A_20251211T094343_20251211T094418_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_008_104_A_025_4005_DHDH_A_20251223T094343_20251223T094418_X05009_N_F_J_001/NISAR_L2_PR_GCOV_008_104_A_025_4005_DHDH_A_20251223T094343_20251223T094418_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_010_104_A_025_4005_DHDH_A_20260116T094344_20260116T094419_X05010_N_F_J_001/NISAR_L2_PR_GCOV_010_104_A_025_4005_DHDH_A_20260116T094344_20260116T094419_X05010_N_F_J_001.h5


## Let's pick track 104 frame 25 and generate a url list output


```python
track = 104
frame = 25
out = f"{os.environ['HOME']}/search_result_httpurls.txt"
cmd = f"seppo_nisar_search --track {track} --frame {frame} --https -o {out}"
print(cmd)
```

    seppo_nisar_search --track 104 --frame 25 --https -o /Users/josefk/search_result_httpurls.txt



```python
nisar_search._main(cmd)
```


```python
! cat $HOME/search_result_httpurls.txt
```

    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_004_104_A_025_4005_DHDH_A_20251105T094341_20251105T094416_X05009_N_F_J_001/NISAR_L2_PR_GCOV_004_104_A_025_4005_DHDH_A_20251105T094341_20251105T094416_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_005_104_A_025_4005_DHDH_A_20251117T094342_20251117T094417_X05009_N_F_J_001/NISAR_L2_PR_GCOV_005_104_A_025_4005_DHDH_A_20251117T094342_20251117T094417_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_006_104_A_025_4005_DHDH_A_20251129T094342_20251129T094417_X05009_N_F_J_001/NISAR_L2_PR_GCOV_006_104_A_025_4005_DHDH_A_20251129T094342_20251129T094417_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_007_104_A_025_4005_DHDH_A_20251211T094343_20251211T094418_X05009_N_F_J_001/NISAR_L2_PR_GCOV_007_104_A_025_4005_DHDH_A_20251211T094343_20251211T094418_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_008_104_A_025_4005_DHDH_A_20251223T094343_20251223T094418_X05009_N_F_J_001/NISAR_L2_PR_GCOV_008_104_A_025_4005_DHDH_A_20251223T094343_20251223T094418_X05009_N_F_J_001.h5
    https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_010_104_A_025_4005_DHDH_A_20260116T094344_20260116T094419_X05010_N_F_J_001/NISAR_L2_PR_GCOV_010_104_A_025_4005_DHDH_A_20260116T094344_20260116T094419_X05010_N_F_J_001.h5


# 2. Data Inspection
## Let's inspect the first link and available VARS 

The `-lg|--list_grids` picks the first url if a list is provided with the `-i <URL_LIST` flag. If you want to inspect a specific url, you can provide the url directly to the `-i <URL>` flag


```python
cmd = f"seppo_nisar_gcov_convert -lg -i {out}"
print(cmd)
```

    seppo_nisar_gcov_convert -lg -i /Users/josefk/search_result_httpurls.txt



```python
nisar_gcov_convert._main(cmd)
```

    ---> Detected Earthdata HTTPS URL. Using Earthdata credentials.
    Starting Batch Processing: 6 files.
    Mode: pwr | Freq: A | Downscale: None
    Inspecting file: https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GCOV_BETA_V1/NISAR_L2_PR_GCOV_004_104_A_025_4005_DHDH_A_20251105T094341_20251105T094416_X05009_N_F_J_001/NISAR_L2_PR_GCOV_004_104_A_025_4005_DHDH_A_20251105T094341_20251105T094416_X05009_N_F_J_001.h5



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


    
    Available Grids in HDF5:
      Frequency A:
        CRS: EPSG:32619
        Raster Size:  35640 x 35208 pixels (cols/rows)
        Resolution: X=10.00, Y=-10.00
        Extent (W,S,E,N): [76320.00, 4970160.00, 432720.00, 5322240.00]
        Footprint (Lon/Lat): (-74.2464, 47.0154), (-71.0728, 47.8317), (-70.0161, 45.7552), (-73.0886, 44.9675)
        Footprint (Native):  (101296.51, 5220242.55), (344882.17, 5299674.42), (420977.22, 5067346.43), (177568.95, 4987472.98)
        Frame Size:          Width: 256.19 km, Height: 244.71 km
        Variables: HHHH, HVHV, mask, numberOfLooks, rtcGammaToSigmaFactor
      Frequency B:
        CRS: EPSG:32619
        Raster Size:  4455 x 4401 pixels (cols/rows)
        Resolution: X=80.00, Y=-80.00
        Extent (W,S,E,N): [76320.00, 4970160.00, 432720.00, 5322240.00]
        Footprint (Lon/Lat): (-74.2464, 47.0154), (-71.0728, 47.8317), (-70.0161, 45.7552), (-73.0886, 44.9675)
        Footprint (Native):  (101296.51, 5220242.55), (344882.17, 5299674.42), (420977.22, 5067346.43), (177568.95, 4987472.98)
        Frame Size:          Width: 256.19 km, Height: 244.71 km
        Variables: HHHH, HVHV, mask, numberOfLooks, rtcGammaToSigmaFactor
    
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

    seppo_nisar_gcov_convert -i /Users/josefk/search_result_httpurls.txt --projwin -72 46 -71.8 45.8 -t_srs 4326 -tr 0.0002 0.0002 -o s3://seppo1-data/NISAR/openSEPPO_testoutput-amp -amp -v



```python
nisar_gcov_convert._main(cmd)
```

    {'cache': None,
     'downscale': None,
     'dualpol_ratio': False,
     'fill_holes': False,
     'freq': 'A',
     'h5': ['/Users/josefk/search_result_httpurls.txt'],
     'input_profile': None,
     'keep_cached': False,
     'list_grids': False,
     'mode': 'AMP',
     'no_tap': False,
     'no_time_series': False,
     'no_vrt': False,
     'output': 's3://seppo1-data/NISAR/openSEPPO_testoutput-amp',
     'output_format': 'COG',
     'output_profile': None,
     'profile': None,
     'projwin': [-72.0, 46.0, -71.8, 45.8],
     'read_threads': 8,
     'rebuild_all_vrts': False,
     'rebuild_only': False,
     'resample': 'cubic',
     'show_vrts': False,
     'single_bands': True,
     'srcwin': None,
     'target_res': [0.0002, 0.0002],
     'target_srs': '4326',
     'use_earthdata': False,
     'vars': None,
     'verbose': True,
     'warp_threads': None}
    ---> Detected Earthdata HTTPS URL. Using Earthdata credentials.
    Starting Batch Processing: 6 files.
    Mode: AMP | Freq: A | Downscale: None
        [t] earthaccess login (cached token, expires 2026-04-29): instant
    Batch Processing Started: 6 files.
    No variables specified. Auto-detecting Covariance variables for Frequency A...



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


      -> Selected: ['HHHH', 'HVHV']
    --> Processing File: NISAR_L2_PR_GCOV_004_104_A_025_4005_DHDH_A_20251105T094341_20251105T094416_X05009_N_F_J_001.h5



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] file open + metadata: 11.1s
        Date: 2025-11-05 | Grid: 10.0m (A) | Mode: h5py
        Reprojecting: EPSG:32619 -> 4326 (resample=cubic)
        Reprojection: expanded native projwin [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155]
        Slice (Map native): [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155] -> Pixels: 19052,22378,1638,2285
        Extracting 2 bands...



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] data read (2×2285×1638, 29.9 MB): 38.9s
        Using explicit target resolution: 0.0002 x 0.0002
        Reprojecting 2 bands...
        Transforming: AMP (Mode: amp)
        Writing separate bands...
        [t] COG write (2 bands, 0.0 MB): 8.1s
        Generated Snapshot VRT: s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_004_104_A_025_4005_DHDH_A_20251105T094341_20251105T094416_X05009_N_F_J_001-EBD_A_hhhv_AMP.vrt
        ✓ Complete (standard (all-bands) mode)
    --> Processing File: NISAR_L2_PR_GCOV_005_104_A_025_4005_DHDH_A_20251117T094342_20251117T094417_X05009_N_F_J_001.h5



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] file open + metadata: 10.2s
        Date: 2025-11-17 | Grid: 10.0m (A) | Mode: h5py
        Reprojecting: EPSG:32619 -> 4326 (resample=cubic)
        Reprojection: expanded native projwin [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155]
        Slice (Map native): [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155] -> Pixels: 19052,22378,1638,2285
        Extracting 2 bands...



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] data read (2×2285×1638, 29.9 MB): 41.8s
        Using explicit target resolution: 0.0002 x 0.0002
        Reprojecting 2 bands...
        Transforming: AMP (Mode: amp)
        Writing separate bands...
        [t] COG write (2 bands, 0.0 MB): 1.9s
        Generated Snapshot VRT: s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_005_104_A_025_4005_DHDH_A_20251117T094342_20251117T094417_X05009_N_F_J_001-EBD_A_hhhv_AMP.vrt
        ✓ Complete (standard (all-bands) mode)
    --> Processing File: NISAR_L2_PR_GCOV_006_104_A_025_4005_DHDH_A_20251129T094342_20251129T094417_X05009_N_F_J_001.h5



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] file open + metadata: 10.4s
        Date: 2025-11-29 | Grid: 10.0m (A) | Mode: h5py
        Reprojecting: EPSG:32619 -> 4326 (resample=cubic)
        Reprojection: expanded native projwin [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155]
        Slice (Map native): [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155] -> Pixels: 19052,22378,1638,2285
        Extracting 2 bands...



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] data read (2×2285×1638, 29.9 MB): 36.1s
        Using explicit target resolution: 0.0002 x 0.0002
        Reprojecting 2 bands...
        Transforming: AMP (Mode: amp)
        Writing separate bands...
        [t] COG write (2 bands, 0.0 MB): 2.0s
        Generated Snapshot VRT: s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_006_104_A_025_4005_DHDH_A_20251129T094342_20251129T094417_X05009_N_F_J_001-EBD_A_hhhv_AMP.vrt
        ✓ Complete (standard (all-bands) mode)
    --> Processing File: NISAR_L2_PR_GCOV_007_104_A_025_4005_DHDH_A_20251211T094343_20251211T094418_X05009_N_F_J_001.h5



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] file open + metadata: 10.5s
        Date: 2025-12-11 | Grid: 10.0m (A) | Mode: h5py
        Reprojecting: EPSG:32619 -> 4326 (resample=cubic)
        Reprojection: expanded native projwin [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155]
        Slice (Map native): [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155] -> Pixels: 19052,22378,1638,2285
        Extracting 2 bands...



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] data read (2×2285×1638, 29.9 MB): 39.4s
        Using explicit target resolution: 0.0002 x 0.0002
        Reprojecting 2 bands...
        Transforming: AMP (Mode: amp)
        Writing separate bands...
        [t] COG write (2 bands, 0.0 MB): 1.7s
        Generated Snapshot VRT: s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_007_104_A_025_4005_DHDH_A_20251211T094343_20251211T094418_X05009_N_F_J_001-EBD_A_hhhv_AMP.vrt
        ✓ Complete (standard (all-bands) mode)
    --> Processing File: NISAR_L2_PR_GCOV_008_104_A_025_4005_DHDH_A_20251223T094343_20251223T094418_X05009_N_F_J_001.h5



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] file open + metadata: 10.6s
        Date: 2025-12-23 | Grid: 10.0m (A) | Mode: h5py
        Reprojecting: EPSG:32619 -> 4326 (resample=cubic)
        Reprojection: expanded native projwin [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155]
        Slice (Map native): [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155] -> Pixels: 19052,22378,1638,2285
        Extracting 2 bands...



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] data read (2×2285×1638, 29.9 MB): 40.6s
        Using explicit target resolution: 0.0002 x 0.0002
        Reprojecting 2 bands...
        Transforming: AMP (Mode: amp)
        Writing separate bands...
        [t] COG write (2 bands, 0.0 MB): 1.8s
        Generated Snapshot VRT: s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_008_104_A_025_4005_DHDH_A_20251223T094343_20251223T094418_X05009_N_F_J_001-EBD_A_hhhv_AMP.vrt
        ✓ Complete (standard (all-bands) mode)
    --> Processing File: NISAR_L2_PR_GCOV_010_104_A_025_4005_DHDH_A_20260116T094344_20260116T094419_X05010_N_F_J_001.h5



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] file open + metadata: 10.5s
        Date: 2026-01-16 | Grid: 10.0m (A) | Mode: h5py
        Reprojecting: EPSG:32619 -> 4326 (resample=cubic)
        Reprojection: expanded native projwin [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155]
        Slice (Map native): [266840.3777043752, 5098454.079643565, 283222.42968315363, 5075609.598720155] -> Pixels: 19052,22378,1638,2285
        Extracting 2 bands...



    QUEUEING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    PROCESSING TASKS | :   0%|          | 0/1 [00:00<?, ?it/s]



    COLLECTING RESULTS | :   0%|          | 0/1 [00:00<?, ?it/s]


        [t] data read (2×2285×1638, 29.9 MB): 39.6s
        Using explicit target resolution: 0.0002 x 0.0002
        Reprojecting 2 bands...
        Transforming: AMP (Mode: amp)
        Writing separate bands...
        [t] COG write (2 bands, 0.0 MB): 7.9s
        Generated Snapshot VRT: s3://seppo1-data/NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_010_104_A_025_4005_DHDH_A_20260116T094344_20260116T094419_X05010_N_F_J_001-EBD_A_hhhv_AMP.vrt
        ✓ Complete (standard (all-bands) mode)
    Generating Time Series VRTs...
      --> VRT: NISAR_L2_PR_GCOV_004_104_A_025_4005_DHDH_A_20251105T000000_20260116T235959_X05009_N_F_J_001-EBD_A_hh_AMP.vrt
      --> VRT: NISAR_L2_PR_GCOV_004_104_A_025_4005_DHDH_A_20251105T000000_20260116T235959_X05009_N_F_J_001-EBD_A_hv_AMP.vrt
    
    Batch Complete. Generated 2 Time Series VRTs.
    
    Building per-track time series VRTs...
      build_track_vrts: 12 TIF files across 1 track(s).
        TS VRT (track 104/A): NISAR_L2_PR_GCOV_004-010_104_A_025_4005_DHDH_A_20251105T094341_20260116T094419-EBD_A_hh_AMP.vrt
        TS VRT (track 104/A): NISAR_L2_PR_GCOV_004-010_104_A_025_4005_DHDH_A_20251105T094341_20260116T094419-EBD_A_hv_AMP.vrt
    
    Bucket: seppo1-data
    
    Single dates:
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_004_104_A_025_4005_DHDH_A_20251105T094341_20251105T094416_X05009_N_F_J_001-EBD_A_hhhv_AMP.vrt
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_005_104_A_025_4005_DHDH_A_20251117T094342_20251117T094417_X05009_N_F_J_001-EBD_A_hhhv_AMP.vrt
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_006_104_A_025_4005_DHDH_A_20251129T094342_20251129T094417_X05009_N_F_J_001-EBD_A_hhhv_AMP.vrt
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_007_104_A_025_4005_DHDH_A_20251211T094343_20251211T094418_X05009_N_F_J_001-EBD_A_hhhv_AMP.vrt
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_008_104_A_025_4005_DHDH_A_20251223T094343_20251223T094418_X05009_N_F_J_001-EBD_A_hhhv_AMP.vrt
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_010_104_A_025_4005_DHDH_A_20260116T094344_20260116T094419_X05010_N_F_J_001-EBD_A_hhhv_AMP.vrt
    
    Time series by track:
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_004-010_104_A_025_4005_DHDH_A_20251105T094341_20260116T094419-EBD_A_hh_AMP.vrt
    NISAR/openSEPPO_testoutput-amp/NISAR_L2_PR_GCOV_004-010_104_A_025_4005_DHDH_A_20251105T094341_20260116T094419-EBD_A_hv_AMP.vrt
    
    Runtime: 5m 48.26s
    


# Display COGs in QGIS

To Display the converted data in QGIS simply choose the `Protocol AWS s3` option, fill in bucket name and one of the VRTs object path from the final output. If you are interested in a simple Timeseries interactive click/plot tool, install from zip our Timeseries SAR plugin from https://github.com/EarthBigData/openSAR/tree/master/code/QGIS/v3/plugins


```python

```
