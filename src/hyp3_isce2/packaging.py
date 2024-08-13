import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from typing import Iterable, Optional

import isce
from hyp3lib.aws import upload_file_to_s3
from hyp3lib.image import create_thumbnail
from lxml import etree
from osgeo import gdal, gdalconst
from pyproj import CRS

import hyp3_isce2
from hyp3_isce2.burst import BurstPosition
from hyp3_isce2.utils import utm_from_lon_lat


@dataclass
class ISCE2Dataset:
    name: str
    suffix: str
    band: Iterable[int]
    dtype: Optional[int] = gdalconst.GDT_Float32


def get_pixel_size(looks: str) -> float:
    return {'20x4': 80.0, '10x2': 40.0, '5x1': 20.0}[looks]


def find_product(pattern: str) -> str:
    """Find a single file within the working directory's structure

    Args:
        pattern: Glob pattern for file

    Returns
        Path to file
    """
    search = Path.cwd().glob(pattern)
    product = str(list(search)[0])
    return product


def get_product_name(reference_scene: str, secondary_scene: str, pixel_spacing: int, slc: bool = True) -> str:
    """Get the name of the interferogram product.

    Args:
        reference_scene: The reference burst name.
        secondary_scene: The secondary burst name.
        pixel_spacing: The spacing of the pixels in the output image.
        slc: Whether the input scenes are SLCs or bursts.

    Returns:
        The name of the interferogram product.
    """

    reference_split = reference_scene.split('_')
    secondary_split = secondary_scene.split('_')

    if slc:
        platform = reference_split[0]
        reference_date = reference_split[5][0:8]
        secondary_date = secondary_split[5][0:8]
        polarization = reference_split[4]
        # TODO: Remove hard code
        polarization = 'VV'
        name_parts = [platform]
    else:
        platform = reference_split[0]
        burst_id = reference_split[1]
        image_plus_swath = reference_split[2]
        reference_date = reference_split[3][0:8]
        secondary_date = secondary_split[3][0:8]
        polarization = reference_split[4]
        name_parts = [platform, burst_id, image_plus_swath]

    product_type = 'INT'
    pixel_spacing = str(int(pixel_spacing))
    product_id = token_hex(2).upper()
    product_name = '_'.join(
        name_parts
        + [
            reference_date,
            secondary_date,
            polarization,
            product_type + pixel_spacing,
            product_id,
        ]
    )

    return product_name


def translate_outputs(product_name: str, pixel_size: float, include_radar: bool = False, use_multilooked=False) -> None:
    """Translate ISCE outputs to a standard GTiff format with a UTM projection.
    Assume you are in the top level of an ISCE run directory

    Args:
        product_name: Name of the product
        pixel_size: Pixel size
        include_radar: Flag to include the full resolution radar geometry products in the output
        use_multilooked: Flag to use multilooked versions of the radar geometry products
    """

    src_ds = gdal.Open('merged/filt_topophase.unw.geo')
    src_geotransform = src_ds.GetGeoTransform()
    src_projection = src_ds.GetProjection()

    target_ds = gdal.Open('merged/dem.crop', gdal.GA_Update)
    target_ds.SetGeoTransform(src_geotransform)
    target_ds.SetProjection(src_projection)

    del src_ds, target_ds

    datasets = [
        ISCE2Dataset('merged/filt_topophase.unw.geo', 'unw_phase', [2]),
        ISCE2Dataset('merged/phsig.cor.geo', 'corr', [1]),
        ISCE2Dataset('merged/dem.crop', 'dem', [1]),
        ISCE2Dataset('merged/filt_topophase.unw.conncomp.geo', 'conncomp', [1]),
    ]

    suffix = '01'
    if use_multilooked:
        suffix += '.multilooked'

    rdr_datasets = [
        ISCE2Dataset(
            find_product(f'fine_interferogram/IW*/burst_{suffix}.int.vrt'),
            'wrapped_phase_rdr',
            [1],
            gdalconst.GDT_CFloat32,
        ),
        ISCE2Dataset(find_product(f'geom_reference/IW*/lat_{suffix}.rdr.vrt'), 'lat_rdr', [1]),
        ISCE2Dataset(find_product(f'geom_reference/IW*/lon_{suffix}.rdr.vrt'), 'lon_rdr', [1]),
        ISCE2Dataset(find_product(f'geom_reference/IW*/los_{suffix}.rdr.vrt'), 'los_rdr', [1, 2]),
    ]
    if include_radar:
        datasets += rdr_datasets

    for dataset in datasets:
        out_file = str(Path(product_name) / f'{product_name}_{dataset.suffix}.tif')
        gdal.Translate(
            destName=out_file,
            srcDS=dataset.name,
            bandList=dataset.band,
            format='GTiff',
            outputType=dataset.dtype,
            noData=0,
            creationOptions=['TILED=YES', 'COMPRESS=LZW', 'NUM_THREADS=ALL_CPUS'],
        )

    # Use numpy.angle to extract the phase component of the complex wrapped interferogram
    wrapped_phase = ISCE2Dataset('filt_topophase.flat.geo', 'wrapped_phase', 1)
    cmd = (
        'gdal_calc.py '
        f'--outfile {product_name}/{product_name}_{wrapped_phase.suffix}.tif '
        f'-A merged/{wrapped_phase.name} --A_band={wrapped_phase.band} '
        '--calc angle(A) --type Float32 --format GTiff --NoDataValue=0 '
        '--creation-option TILED=YES --creation-option COMPRESS=LZW --creation-option NUM_THREADS=ALL_CPUS'
    )
    subprocess.run(cmd.split(' '), check=True)

    ds = gdal.Open('merged/los.rdr.geo', gdal.GA_Update)
    ds.GetRasterBand(1).SetNoDataValue(0)
    ds.GetRasterBand(2).SetNoDataValue(0)
    del ds

    # Performs the inverse of the operation performed by MintPy:
    # https://github.com/insarlab/MintPy/blob/df96e0b73f13cc7e2b6bfa57d380963f140e3159/src/mintpy/objects/stackDict.py#L732-L737
    # First subtract the incidence angle from ninety degrees to go from sensor-to-ground to ground-to-sensor,
    # then convert to radians
    incidence_angle = ISCE2Dataset('los.rdr.geo', 'lv_theta', 1)
    cmd = (
        'gdal_calc.py '
        f'--outfile {product_name}/{product_name}_{incidence_angle.suffix}.tif '
        f'-A merged/{incidence_angle.name} --A_band={incidence_angle.band} '
        '--calc (90-A)*pi/180 --type Float32 --format GTiff --NoDataValue=0 '
        '--creation-option TILED=YES --creation-option COMPRESS=LZW --creation-option NUM_THREADS=ALL_CPUS'
    )
    subprocess.run(cmd.split(' '), check=True)

    # Performs the inverse of the operation performed by MintPy:
    # https://github.com/insarlab/MintPy/blob/df96e0b73f13cc7e2b6bfa57d380963f140e3159/src/mintpy/objects/stackDict.py#L739-L745
    # First add ninety degrees to the azimuth angle to go from angle-from-east to angle-from-north,
    # then convert to radians
    azimuth_angle = ISCE2Dataset('los.rdr.geo', 'lv_phi', 2)
    cmd = (
        'gdal_calc.py '
        f'--outfile {product_name}/{product_name}_{azimuth_angle.suffix}.tif '
        f'-A merged/{azimuth_angle.name} --A_band={azimuth_angle.band} '
        '--calc (90+A)*pi/180 --type Float32 --format GTiff --NoDataValue=0 '
        '--creation-option TILED=YES --creation-option COMPRESS=LZW --creation-option NUM_THREADS=ALL_CPUS'
    )
    subprocess.run(cmd.split(' '), check=True)

    ds = gdal.Open('merged/filt_topophase.unw.geo')
    geotransform = ds.GetGeoTransform()
    del ds

    epsg = utm_from_lon_lat(geotransform[0], geotransform[3])
    files = [str(path) for path in Path(product_name).glob('*.tif') if not path.name.endswith('rdr.tif')]
    for file in files:
        gdal.Warp(
            file,
            file,
            dstSRS=f'epsg:{epsg}',
            creationOptions=['TILED=YES', 'COMPRESS=LZW', 'NUM_THREADS=ALL_CPUS'],
            xRes=pixel_size,
            yRes=pixel_size,
            targetAlignedPixels=True,
        )


def convert_raster_from_isce2_gdal(input_image, ref_image, output_image):
    """Convert the water mask in WGS84 to be the same projection and extent of the output product.

    Args:
        input_image: dem file name
        ref_image: output geotiff file name
        output_image: water mask file name
    """

    ref_ds = gdal.Open(ref_image)

    gt = ref_ds.GetGeoTransform()

    pixel_size = gt[1]

    minx = gt[0]
    maxx = gt[0] + gt[1] * ref_ds.RasterXSize
    maxy = gt[3]
    miny = gt[3] + gt[5] * ref_ds.RasterYSize

    crs = ref_ds.GetSpatialRef()
    epsg = CRS.from_wkt(crs.ExportToWkt()).to_epsg()

    del ref_ds

    gdal.Warp(
        output_image,
        input_image,
        dstSRS=f'epsg:{epsg}',
        creationOptions=['TILED=YES', 'COMPRESS=LZW', 'NUM_THREADS=ALL_CPUS'],
        outputBounds=[minx, miny, maxx, maxy],
        xRes=pixel_size,
        yRes=pixel_size,
        targetAlignedPixels=True,
    )


def water_mask(unwrapped_phase: str, water_mask: str) -> None:
    """Apply the water mask to the unwrapped phase

    Args:
        unwrapped_phase: The unwrapped phase file
        water_mask: The water mask file
    """

    convert_raster_from_isce2_gdal('water_mask.wgs84', unwrapped_phase, water_mask)
    cmd = (
        'gdal_calc.py '
        f'--outfile {unwrapped_phase} '
        f'-A {unwrapped_phase} -B {water_mask} '
        '--calc A*B '
        '--overwrite '
        '--NoDataValue 0 '
        '--creation-option TILED=YES --creation-option COMPRESS=LZW --creation-option NUM_THREADS=ALL_CPUS'
    )
    subprocess.run(cmd.split(' '), check=True)


class GDALConfigManager:
    """Context manager for setting GDAL config options temporarily"""

    def __init__(self, **options):
        """
        Args:
            **options: GDAL Config `option=value` keyword arguments.
        """
        self.options = options.copy()
        self._previous_options = {}

    def __enter__(self):
        for key in self.options:
            self._previous_options[key] = gdal.GetConfigOption(key)

        for key, value in self.options.items():
            gdal.SetConfigOption(key, value)

    def __exit__(self, exc_type, exc_val, exc_tb):
        for key, value in self._previous_options.items():
            gdal.SetConfigOption(key, value)


def make_browse_image(input_tif: str, output_png: str) -> None:
    with GDALConfigManager(GDAL_PAM_ENABLED='NO'):
        stats = gdal.Info(input_tif, format='json', stats=True)['stac']['raster:bands'][0]['stats']
        gdal.Translate(
            destName=output_png,
            srcDS=input_tif,
            format='png',
            outputType=gdal.GDT_Byte,
            width=2048,
            strict=True,
            scaleParams=[[stats['minimum'], stats['maximum']]],
        )


def make_readme(
    product_dir: Path,
    product_name: str,
    reference_scene: str,
    secondary_scene: str,
    range_looks: int,
    azimuth_looks: int,
    apply_water_mask: bool,
) -> None:
    wrapped_phase_path = product_dir / f'{product_name}_wrapped_phase.tif'
    info = gdal.Info(str(wrapped_phase_path), format='json')
    secondary_granule_datetime_str = secondary_scene.split('_')[3]

    payload = {
        'processing_date': datetime.now(timezone.utc),
        'plugin_name': hyp3_isce2.__name__,
        'plugin_version': hyp3_isce2.__version__,
        'processor_name': isce.__name__.upper(),
        'processor_version': isce.__version__,
        'projection': hyp3_isce2.metadata.util.get_projection(info['coordinateSystem']['wkt']),
        'pixel_spacing': info['geoTransform'][1],
        'product_name': product_name,
        'reference_burst_name': reference_scene,
        'secondary_burst_name': secondary_scene,
        'range_looks': range_looks,
        'azimuth_looks': azimuth_looks,
        'secondary_granule_date': datetime.strptime(secondary_granule_datetime_str, '%Y%m%dT%H%M%S'),
        'dem_name': 'GLO-30',
        'dem_pixel_spacing': '30 m',
        'apply_water_mask': apply_water_mask,
    }
    content = hyp3_isce2.metadata.util.render_template('insar_burst/insar_burst_readme.md.txt.j2', payload)

    output_file = product_dir / f'{product_name}_README.md.txt'
    with open(output_file, 'w') as f:
        f.write(content)


@dataclass
class ParameterFile:
    reference_granule: str
    secondary_granule: str
    reference_orbit_direction: str
    reference_orbit_number: str
    secondary_orbit_direction: str
    secondary_orbit_number: str
    baseline: float
    utc_time: float
    heading: float
    spacecraft_height: float
    earth_radius_at_nadir: float
    slant_range_near: float
    slant_range_center: float
    slant_range_far: float
    range_looks: int
    azimuth_looks: int
    insar_phase_filter: bool
    phase_filter_parameter: float
    range_bandpass_filter: bool
    azimuth_bandpass_filter: bool
    dem_source: str
    dem_resolution: int
    unwrapping_type: str
    speckle_filter: bool
    water_mask: bool
    radar_n_lines: Optional[int] = None
    radar_n_samples: Optional[int] = None
    radar_first_valid_line: Optional[int] = None
    radar_n_valid_lines: Optional[int] = None
    radar_first_valid_sample: Optional[int] = None
    radar_n_valid_samples: Optional[int] = None
    multilook_azimuth_time_interval: Optional[float] = None
    multilook_range_pixel_size: Optional[float] = None
    radar_sensing_stop: Optional[datetime] = None

    def __str__(self):
        output_strings = [
            f'Reference Granule: {self.reference_granule}\n',
            f'Secondary Granule: {self.secondary_granule}\n',
            f'Reference Pass Direction: {self.reference_orbit_direction}\n',
            f'Reference Orbit Number: {self.reference_orbit_number}\n',
            f'Secondary Pass Direction: {self.secondary_orbit_direction}\n',
            f'Secondary Orbit Number: {self.secondary_orbit_number}\n',
            f'Baseline: {self.baseline}\n',
            f'UTC time: {self.utc_time}\n',
            f'Heading: {self.heading}\n',
            f'Spacecraft height: {self.spacecraft_height}\n',
            f'Earth radius at nadir: {self.earth_radius_at_nadir}\n',
            f'Slant range near: {self.slant_range_near}\n',
            f'Slant range center: {self.slant_range_center}\n',
            f'Slant range far: {self.slant_range_far}\n',
            f'Range looks: {self.range_looks}\n',
            f'Azimuth looks: {self.azimuth_looks}\n',
            f'INSAR phase filter: {"yes" if self.insar_phase_filter else "no"}\n',
            f'Phase filter parameter: {self.phase_filter_parameter}\n',
            f'Range bandpass filter: {"yes" if self.range_bandpass_filter else "no"}\n',
            f'Azimuth bandpass filter: {"yes" if self.azimuth_bandpass_filter else "no"}\n',
            f'DEM source: {self.dem_source}\n',
            f'DEM resolution (m): {self.dem_resolution}\n',
            f'Unwrapping type: {self.unwrapping_type}\n',
            f'Speckle filter: {"yes" if self.speckle_filter else "no"}\n',
            f'Water mask: {"yes" if self.water_mask else "no"}\n',
        ]

        # TODO could use a more robust way to check if radar data is present
        if self.radar_n_lines:
            radar_data = [
                f'Radar n lines: {self.radar_n_lines}\n',
                f'Radar n samples: {self.radar_n_samples}\n',
                f'Radar first valid line: {self.radar_first_valid_line}\n',
                f'Radar n valid lines: {self.radar_n_valid_lines}\n',
                f'Radar first valid sample: {self.radar_first_valid_sample}\n',
                f'Radar n valid samples: {self.radar_n_valid_samples}\n',
                f'Multilook azimuth time interval: {self.multilook_azimuth_time_interval}\n',
                f'Multilook range pixel size: {self.multilook_range_pixel_size}\n',
                f'Radar sensing stop: {datetime.strftime(self.radar_sensing_stop, "%Y-%m-%dT%H:%M:%S.%f")}\n',
            ]
            output_strings += radar_data

        return ''.join(output_strings)

    def __repr__(self):
        return self.__str__()

    def write(self, out_path: Path):
        out_path.write_text(self.__str__())


def make_parameter_file(
    out_path: Path,
    reference_scene: str,
    secondary_scene: str,
    azimuth_looks: int,
    range_looks: int,
    apply_water_mask: bool,
    multilook_position: Optional[BurstPosition] = None,
    swath_number: Optional[int] = None,
    dem_name: str = 'GLO_30',
    dem_resolution: int = 30,
) -> None:
    """Create a parameter file for the output product

    Args:
        out_path: path to output the parameter file
        reference_scene: Reference burst name
        secondary_scene: Secondary burst name
        azimuth_looks: Number of azimuth looks
        range_looks: Number of range looks
        swath_number: Number of swath to grab bursts from (1, 2, or 3) for IW
        multilook_position: Burst position for multilooked radar geometry products
        dem_name: Name of the DEM that is use
        dem_resolution: Resolution of the DEM

    returns:
        None
    """
    SPEED_OF_LIGHT = 299792458.0
    SPACECRAFT_HEIGHT = 693000.0
    EARTH_RADIUS = 6337286.638938101

    parser = etree.XMLParser(encoding='utf-8', recover=True)

    ref_tag = reference_scene[-10:-6]
    sec_tag = secondary_scene[-10:-6]
    reference_safe = [file for file in os.listdir('.') if file.endswith(f'{ref_tag}.SAFE')][0]
    secondary_safe = [file for file in os.listdir('.') if file.endswith(f'{sec_tag}.SAFE')][0]

    ref_annotation_path = f'{reference_safe}/annotation/'
    ref_annotation = [file for file in os.listdir(ref_annotation_path) if os.path.isfile(ref_annotation_path + file)][0]

    ref_manifest_xml = etree.parse(f'{reference_safe}/manifest.safe', parser)
    sec_manifest_xml = etree.parse(f'{secondary_safe}/manifest.safe', parser)
    ref_annotation_xml = etree.parse(f'{ref_annotation_path}{ref_annotation}', parser)
    topsProc_xml = etree.parse('topsProc.xml', parser)
    topsApp_xml = etree.parse('topsApp.xml', parser)

    safe = '{http://www.esa.int/safe/sentinel-1.0}'
    s1 = '{http://www.esa.int/safe/sentinel-1.0/sentinel-1}'
    metadata_path = './/metadataObject[@ID="measurementOrbitReference"]//xmlData//'
    orbit_number_query = metadata_path + safe + 'orbitNumber'
    orbit_direction_query = metadata_path + safe + 'extension//' + s1 + 'pass'

    ref_orbit_number = ref_manifest_xml.find(orbit_number_query).text
    ref_orbit_direction = ref_manifest_xml.find(orbit_direction_query).text
    sec_orbit_number = sec_manifest_xml.find(orbit_number_query).text
    sec_orbit_direction = sec_manifest_xml.find(orbit_direction_query).text
    ref_heading = float(ref_annotation_xml.find('.//platformHeading').text)
    ref_time = ref_annotation_xml.find('.//productFirstLineUtcTime').text
    slant_range_time = float(ref_annotation_xml.find('.//slantRangeTime').text)
    range_sampling_rate = float(ref_annotation_xml.find('.//rangeSamplingRate').text)
    number_samples = int(ref_annotation_xml.find('.//swathTiming/samplesPerBurst').text)
    baseline_perp = topsProc_xml.find(f'.//IW-{swath_number}_Bperp_at_midrange_for_first_common_burst').text
    unwrapper_type = topsApp_xml.find('.//property[@name="unwrapper name"]').text
    phase_filter_strength = topsApp_xml.find('.//property[@name="filter strength"]').text

    slant_range_near = float(slant_range_time) * SPEED_OF_LIGHT / 2
    range_pixel_spacing = SPEED_OF_LIGHT / (2 * range_sampling_rate)
    slant_range_far = slant_range_near + (number_samples - 1) * range_pixel_spacing
    slant_range_center = (slant_range_near + slant_range_far) / 2

    s = ref_time.split('T')[1].split(':')
    utc_time = ((int(s[0]) * 60 + int(s[1])) * 60) + float(s[2])

    parameter_file = ParameterFile(
        reference_granule=reference_scene,
        secondary_granule=secondary_scene,
        reference_orbit_direction=ref_orbit_direction,
        reference_orbit_number=ref_orbit_number,
        secondary_orbit_direction=sec_orbit_direction,
        secondary_orbit_number=sec_orbit_number,
        baseline=float(baseline_perp),
        utc_time=utc_time,
        heading=ref_heading,
        spacecraft_height=SPACECRAFT_HEIGHT,
        earth_radius_at_nadir=EARTH_RADIUS,
        slant_range_near=slant_range_near,
        slant_range_center=slant_range_center,
        slant_range_far=slant_range_far,
        range_looks=int(range_looks),
        azimuth_looks=int(azimuth_looks),
        insar_phase_filter=True,
        phase_filter_parameter=float(phase_filter_strength),
        range_bandpass_filter=False,
        azimuth_bandpass_filter=False,
        dem_source=dem_name,
        dem_resolution=dem_resolution,
        unwrapping_type=unwrapper_type,
        speckle_filter=True,
        water_mask=apply_water_mask,
        radar_n_lines=multilook_position.n_lines,
        radar_n_samples=multilook_position.n_samples,
        radar_first_valid_line=multilook_position.first_valid_line,
        radar_n_valid_lines=multilook_position.n_valid_lines,
        radar_first_valid_sample=multilook_position.first_valid_sample,
        radar_n_valid_samples=multilook_position.n_valid_samples,
        multilook_azimuth_time_interval=multilook_position.azimuth_time_interval,
        multilook_range_pixel_size=multilook_position.range_pixel_size,
        radar_sensing_stop=multilook_position.sensing_stop,
    )
    parameter_file.write(out_path)


def upload_product_to_s3(product_dir, output_zip, bucket, bucket_prefix):
    for browse in product_dir.glob('*.png'):
        create_thumbnail(browse, output_dir=product_dir)

    upload_file_to_s3(Path(output_zip), bucket, bucket_prefix)

    for product_file in product_dir.iterdir():
        upload_file_to_s3(product_file, bucket, bucket_prefix)