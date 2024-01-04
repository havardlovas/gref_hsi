import cv2 as cv
import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np

import pyproj
from pyproj import CRS, Transformer
import pyvista as pv

import rasterio
from rasterio.features import geometry_mask
from rasterio.warp import calculate_default_transform, reproject, Resampling

from osgeo import gdal, osr
from shapely.geometry import Polygon, mapping
from sklearn.neighbors import NearestNeighbors
from spectral import envi
import os

# Local module
from scripts.colours import Image as Imcol

GRAVITY = 9.81 # m/s^2


class GeoSpatialAbstractionHSI():
    def __init__(self, point_cloud, transect_string, config):
        self.config = config
        self.name = transect_string
        self.points_geocsc = point_cloud
        self.is_global = self.config['Coordinate Reference Systems']['geocsc_epsg_export'] != 'Local'
        if self.is_global:
            self.epsg_geocsc = int(config['Coordinate Reference Systems']['geocsc_epsg_export'])

            self.epsg_proj = int(config['Coordinate Reference Systems']['proj_epsg'])
    def transform_geocentric_to_projected(self):
        self.points_proj  = self.points_geocsc # Remains same if it is local
        if self.is_global:
            geocsc = CRS.from_epsg(self.epsg_geocsc)
            proj = CRS.from_epsg(self.epsg_proj)
            transformer = Transformer.from_crs(geocsc, proj)

            xECEF = self.points_geocsc[:,:,0].reshape((-1, 1))
            yECEF = self.points_geocsc[:, :, 1].reshape((-1, 1))
            zECEF = self.points_geocsc[:, :, 2].reshape((-1, 1))

            self.offX = float(self.config['General']['offsetX'])
            self.offY = float(self.config['General']['offsetY'])
            self.offZ = float(self.config['General']['offsetZ'])

            (east, north, hei) = transformer.transform(xx=xECEF + self.offX, yy=yECEF + self.offY, zz=zECEF + self.offZ)

            self.points_proj[:,:,0] = east.reshape((self.points_proj.shape[0], self.points_proj.shape[1]))
            self.points_proj[:, :, 1] = north.reshape((self.points_proj.shape[0], self.points_proj.shape[1]))
            self.points_proj[:, :, 2] = hei.reshape((self.points_proj.shape[0], self.points_proj.shape[1]))


    def footprint_to_shape_file(self):
        self.edge_start = self.points_proj[0, :, 0:2].reshape((-1,2))
        self.edge_end = self.points_proj[-1, :, 0:2].reshape((-1,2))
        self.side_1 = self.points_proj[:, 0, 0:2].reshape((-1,2))
        self.side_2 = self.points_proj[:, -1, 0:2].reshape((-1,2))


        # Do it clockwise
        self.hull_line = np.concatenate((
            self.edge_start,
            self.side_2,
            np.flip(self.edge_end, axis=0),
            np.flip(self.side_1, axis = 0)

        ), axis = 0)


        self.footprint_shp = Polygon(self.hull_line)

        if self.is_global:
            self.crs = 'EPSG:' + str(self.epsg_proj)
        else:
            # Our frame is a local engineering frame (local tangent plane)
            wkt = self.config['Coordinate Reference Systems']['wktLocal']
            #ellps = self.config['Coordinate Reference Systems']['ellps']
            #geo_dict = {'proj':'utm', 'zone': 10, 'ellps': ellps}
            #self.crs = pyproj.CRS.from_dict(proj_dict=geo_dict)
            self.crs = pyproj.CRS.from_wkt(wkt)

        gdf = gpd.GeoDataFrame(geometry=[self.footprint_shp], crs=self.crs)

        shape_path = self.config['Absolute Paths']['footPrintPaths'] + self.name + '.shp'

        gdf.to_file(shape_path, driver='ESRI Shapefile')
    def resample_datacube(self, hyp, rgb_composite_only):
        #
        self.res = float(self.config['Orthorectification']['resolutionHyperspectralMosaic'])
        wl_red = float(self.config['General']['RedWavelength'])
        wl_green = float(self.config['General']['GreenWavelength'])
        wl_blue = float(self.config['General']['BlueWavelength'])

        rgb_composite_path = self.config['Absolute Paths']['rgbCompositePaths']
        datacube_path = self.config['Absolute Paths']['orthoCubePaths']
        resamplingMethod = self.config['Orthorectification']['resamplingMethod']
        
        # The footprint-shape is a in a vectorized format and needs to be mapped into a raster-mask
        xmin, ymin, xmax, ymax = self.footprint_shp.bounds
        width = int((xmax - xmin) / self.res)
        height = int((ymax - ymin) / self.res)
        transform = rasterio.transform.from_bounds(xmin, ymin, xmax, ymax, width, height)

        # Create mask from the polygon
        geoms = [mapping(self.footprint_shp)]
        mask = geometry_mask(geoms, out_shape=(height, width), transform=transform)

        # Set custom RGB bands from *.ini file
        wavelength_nm = np.array([wl_red, wl_green, wl_blue])
        band_ind_R = np.argmin(np.abs(wavelength_nm[0] - hyp.band2Wavelength))
        band_ind_G = np.argmin(np.abs(wavelength_nm[1] - hyp.band2Wavelength))
        band_ind_B = np.argmin(np.abs(wavelength_nm[2] - hyp.band2Wavelength))
        n_bands = len(hyp.band2Wavelength)
        wavelengths=hyp.band2Wavelength

        
        
        datacube = hyp.dataCubeRadiance[:, :, :].reshape((-1, n_bands))
        rgb_cube = datacube[:, [band_ind_R, band_ind_G, band_ind_B]].reshape((-1, 3))

        transform = rasterio.transform.from_bounds(xmin, ymin, xmax, ymax, width, height)

        del hyp

        # Horizontal coordinates of intersections
        coords = self.points_proj[:, :, 0:2].reshape((-1, 2))
        if resamplingMethod == 'Nearest':
            
            tree = NearestNeighbors(radius=self.res).fit(coords)
            print('Finalized NN interpolation')
            xi, yi = np.meshgrid(np.linspace(xmin, xmax, width), np.linspace(ymin, ymax, height))
            xy = np.vstack((xi.flatten(), yi.flatten())).T
            dist, indexes = tree.kneighbors(xy, 1)

            # Build the RGB cube from the indices

            print('Reforming RGB data')
            ortho_rgb = rgb_cube[indexes, :].flatten()
            ortho_rgb = np.flip(ortho_rgb.reshape((height, width, 3)).astype(np.float64), axis = 0)
            print(' RGB Reformed data')
            # Build datacube
            if not rgb_composite_only:
                ortho_datacube = datacube[indexes, :]
                ortho_datacube = np.flip(ortho_datacube.reshape((height, width, n_bands)).astype(np.float64), axis=0)

            
            

            self.width_rectified = width
            self.height_rectified = height
            self.indexes = indexes




        # Set nodata value
        nodata = -9999
        ortho_rgb[mask == 1, :] = nodata
        # Arange datacube or composite in rasterio-friendly structure
        ortho_rgb = np.transpose(ortho_rgb, axes = [2, 0, 1])

        if not rgb_composite_only:
            ortho_datacube[mask == 1, :] = nodata

        
        


        # Write pseudo-RGB composite to composite folder ../GIS/RGBComposites
        with rasterio.open(rgb_composite_path + self.name + '.tif', 'w', driver='GTiff',
                                height=height, width=width, count=3, dtype=np.float64,
                                crs=self.crs, transform=transform, nodata=nodata) as dst:

            dst.write(ortho_rgb)
        # Write ENVI-style hyperspectral datacube
        if rgb_composite_only == False:
            ortho_datacube = np.transpose(ortho_datacube, axes=[2, 0, 1])
            self.write_datacube_ENVI(ortho_datacube, nodata, transform, datacube_path = datacube_path + self.name, wavelengths=wavelengths)




        else:
            print('You are only writing parts of a datacube')

    def write_datacube_ENVI(self, ortho_datacube, nodata, transform, datacube_path, wavelengths):
        nx = ortho_datacube.shape[1]
        mx = ortho_datacube.shape[2]
        k = ortho_datacube.shape[0]

        # Create the bsq file
        with rasterio.open(datacube_path + '.bsq', 'w', driver='ENVI', height=nx, width=mx, count=k, crs=self.crs, dtype=ortho_datacube[0].dtype, transform=transform , nodata=nodata) as dst:
            for i, band_data in enumerate(ortho_datacube, start=1):
                dst.write(band_data, i)


        # Make some simple modifications
        data_file_path = datacube_path + '.bsq'



        # Include meta data regarding the unit
        unit_str = self.config['General']['radiometric_unit']
        header_file_path = datacube_path + '.hdr'
        header = envi.open(header_file_path)
        # Set the unit of the signal in the header
        header.metadata['unit'] = unit_str
        # Set wavelengths array in the header
        header.bands.centers = wavelengths
        # TODO: include support for the bandwidths
        # header.bands.bandwidths = wl
        envi.save_image(header_file_path, header, metadata={}, interleave='bsq', filename=data_file_path, force=True)


        os.remove(datacube_path + '.img')


    def compare_hsi_composite_with_rgb_mosaic(self):
        self.rgb_ortho_path = self.config['Absolute Paths']['rgbOrthoPath']
        self.hsi_composite = self.config['Absolute Paths']['rgbCompositePaths'] + self.name + '.tif'
        self.rgb_ortho_reshaped = self.config['Absolute Paths']['rgbOrthoReshaped'] + self.name + '.tif'
        self.dem_path = self.config['Absolute Paths']['demPath']
        self.dem_reshaped = self.config['Absolute Paths']['demReshaped'] + self.name + '_dem.tif'


        self.resample_rgb_ortho_to_hsi_ortho()

        self.resample_dem_to_hsi_ortho()


        raster_rgb = gdal.Open(self.rgb_ortho_reshaped, gdal.GA_Update)
        xoff1, a1, b1, yoff1, d1, e1 = raster_rgb.GetGeoTransform()  # This should be equal
        raster_rgb_array = np.array(raster_rgb.ReadAsArray())
        R = raster_rgb_array[0, :, :].reshape((raster_rgb_array.shape[1], raster_rgb_array.shape[2], 1))
        G = raster_rgb_array[1, :, :].reshape((raster_rgb_array.shape[1], raster_rgb_array.shape[2], 1))
        B = raster_rgb_array[2, :, :].reshape((raster_rgb_array.shape[1], raster_rgb_array.shape[2], 1))
        # del raster_array1
        ortho_rgb = np.concatenate((R, G, B), axis=2)
        rgb_image = Imcol(ortho_rgb)

        raster_hsi = gdal.Open(self.hsi_composite)
        raster_hsi_array = np.array(raster_hsi.ReadAsArray())
        xoff2, a2, b2, yoff2, d2, e2 = raster_hsi.GetGeoTransform()
        self.transform_pixel_projected = raster_hsi.GetGeoTransform()
        R = raster_hsi_array[0, :, :].reshape((raster_hsi_array.shape[1], raster_hsi_array.shape[2], 1))
        G = raster_hsi_array[1, :, :].reshape((raster_hsi_array.shape[1], raster_hsi_array.shape[2], 1))
        B = raster_hsi_array[2, :, :].reshape((raster_hsi_array.shape[1], raster_hsi_array.shape[2], 1))

        ortho_hsi = np.concatenate((R, G, B), axis=2)

        max_val = np.percentile(ortho_hsi.reshape(-1), 99)
        ortho_hsi /= max_val
        ortho_hsi[ortho_hsi > 1] = 1
        ortho_hsi = (ortho_hsi * 255).astype(np.uint8)
        ortho_hsi[ortho_hsi == 0] = 255
        hsi_image = Imcol(ortho_hsi)


        # Dem
        self.raster_dem = rasterio.open(self.dem_reshaped)


        # Adjust Clahe
        hsi_image.clahe_adjustment()
        rgb_image.clahe_adjustment()

        hsi_image.to_luma(gamma=False, image_array = hsi_image.clahe_adjusted)
        rgb_image.to_luma(gamma=False, image_array= rgb_image.clahe_adjusted)

        self.compute_sift_difference(hsi_image.luma_array, rgb_image.luma_array)



    def resample_rgb_ortho_to_hsi_ortho(self):
        """Reproject RGB orthophoto to match the shape and projection of HSI raster.

        Parameters
        ----------
        infile : (string) path to input file to reproject
        match : (string) path to raster with desired shape and projection
        outfile : (string) path to output file tif
        """

        infile = self.rgb_ortho_path
        match = self.hsi_composite
        outfile = self.rgb_ortho_reshaped
        # open input
        with rasterio.open(infile) as src:
            src_transform = src.transform

            # open input to match
            with rasterio.open(match) as match:
                dst_crs = match.crs

                # calculate the output transform matrix
                dst_transform, dst_width, dst_height = calculate_default_transform(
                    src.crs,  # input CRS
                    dst_crs,  # output CRS
                    match.width,  # input width
                    match.height,  # input height
                    *match.bounds,  # unpacks input outer boundaries (left, bottom, right, top)
                )

            # set properties for output
            dst_kwargs = src.meta.copy()
            dst_kwargs.update({"crs": dst_crs,
                               "transform": dst_transform,
                               "width": dst_width,
                               "height": dst_height,
                               "nodata": 0})
            #print("Coregistered to shape:", dst_height, dst_width, '\n Affine', dst_transform)
            # open output
            with rasterio.open(outfile, "w", **dst_kwargs) as dst:
                # iterate through bands and write using reproject function
                for i in range(1, src.count + 1):
                    reproject(
                        source=rasterio.band(src, i),
                        destination=rasterio.band(dst, i),
                        src_transform=src.transform,
                        src_crs=src.crs,
                        dst_transform=dst_transform,
                        dst_crs=dst_crs,
                        resampling=Resampling.cubic)

    def resample_dem_to_hsi_ortho(self):
        """Reproject a file to match the shape and projection of existing raster.

        Parameters
        ----------
        infile : (string) path to input file to reproject
        match : (string) path to raster with desired shape and projection
        outfile : (string) path to output file tif
        """

        infile = self.dem_path
        match = self.hsi_composite
        outfile = self.dem_reshaped
        # open input
        with rasterio.open(infile) as src:
            src_transform = src.transform

            # open input to match
            with rasterio.open(match) as match:
                dst_crs = match.crs

                # calculate the output transform matrix
                dst_transform, dst_width, dst_height = calculate_default_transform(
                    src.crs,  # input CRS
                    dst_crs,  # output CRS
                    match.width,  # input width
                    match.height,  # input height
                    *match.bounds,  # unpacks input outer boundaries (left, bottom, right, top)
                )

            # set properties for output
            dst_kwargs = src.meta.copy()
            dst_kwargs.update({"crs": dst_crs,
                               "transform": dst_transform,
                               "width": dst_width,
                               "height": dst_height,
                               "nodata": 0})
            #print("Coregistered to shape:", dst_height, dst_width, '\n Affine', dst_transform)
            # open output
            with rasterio.open(outfile, "w", **dst_kwargs) as dst:
                # iterate through bands and write using reproject function
                for i in range(1, src.count + 1):
                    reproject(
                        source=rasterio.band(src, i),
                        destination=rasterio.band(dst, i),
                        src_transform=src.transform,
                        src_crs=src.crs,
                        dst_transform=dst_transform,
                        dst_crs=dst_crs,
                        resampling=Resampling.cubic)


    def compute_sift_difference(self, gray1, gray2):
        gray1 = (gray1 - np.min(gray1)) / (np.max(gray1) - np.min(gray1))
        gray2 = (gray2 - np.min(gray2)) / (np.max(gray2) - np.min(gray2))

        gray1 = (gray1 * 255).astype(np.uint8)
        gray2 = (gray2 * 255).astype(np.uint8)


        # Find the keypoints and descriptors with SIFT
        sift = cv.SIFT_create()
        kp2, des2 = sift.detectAndCompute(gray2, None)
        print('Key points found')
        kp1, des1 = sift.detectAndCompute(gray1, None)
        FLANN_INDEX_KDTREE = 1
        index_params = dict(algorithm=FLANN_INDEX_KDTREE, trees=5)
        search_params = dict(checks=50)
        flann = cv.FlannBasedMatcher(index_params, search_params)
        matches = flann.knnMatch(des1, des2, k=2)
        # store all the good matches as per Lowe's ratio test. We changed 0.8 to 0.85 to get more matches
        good = []
        for m, n in matches:
            if m.distance < 0.80 * n.distance:
                good.append(m)

        draw_params = dict(matchColor=(0, 255, 0),  # draw matches in green color
                           flags=2)

        diff_u = np.zeros(len(good))
        diff_v = np.zeros(len(good))
        uv_vec_hsi = np.zeros((len(good), 2))
        uv_vec_rgb = np.zeros((len(good), 2))
        for i in range(len(good)):
            idx2 = good[i].trainIdx
            idx1 = good[i].queryIdx
            uv1 = kp1[idx1].pt  # Slit image
            uv2 = kp2[idx2].pt  # Orthomosaic
            uv_vec_hsi[i,:] = uv1
            uv_vec_rgb[i,:] = uv2

            ## Conversion to global coordinates
            diff_u[i] = uv2[0] - uv1[0]
            diff_v[i] = uv2[1] - uv1[1]

        img3 = cv.drawMatches(gray1, kp1, gray2, kp2, good, None, **draw_params)
        plt.imshow(img3, 'gray')
        plt.show()

        #print(len(good))

        med_u = np.median(diff_u[np.abs(diff_u) < 10])
        med_v = np.median(diff_v[np.abs(diff_u) < 10])
#
        #print(np.mean(np.abs(diff_u[np.abs(diff_u) < 100])))
        #print(np.mean(np.abs(diff_v[np.abs(diff_u) < 100])))
##
        #print(np.median(np.abs(diff_u[np.abs(diff_u) < 100]  - med_u)))
        #print(np.median(np.abs(diff_v[np.abs(diff_u) < 100] - med_v)))
##
        MAD_u = np.median(np.abs(diff_u[np.abs(diff_u) < 100]  - med_u))
        MAD_v = np.median(np.abs(diff_v[np.abs(diff_u) < 100] - med_v))
##
        #MAD_tot = np.median(np.sqrt((diff_v[np.abs(diff_u) < 100] - med_v)**2 + (diff_u[np.abs(diff_u) < 100] - med_u)**2))
        # IF the disagreement is more than 100 pixels, omit it
        diff = np.sqrt(diff_u ** 2 + diff_v ** 2)

        MAE_tot = np.median(diff[diff < 5])
        print(len(good))
        print(MAE_tot)
        self.feature_uv_hsi = uv_vec_hsi[diff < 5, :]
        self.feature_uv_rgb = uv_vec_rgb[diff < 5, :]
        print(len(self.feature_uv_rgb))
#
        #print(med_u)
        #print(med_v)
        #print(MAD_u)
        #print(MAD_v)

        #plt.imshow(gray1)





        #plt.scatter(uv_vec[:,0][np.abs(diff_u) < 100], uv_vec[:,1][np.abs(diff_u) < 100], c = diff_u[np.abs(diff_u) < 100] - np.median(diff_u[np.abs(diff_u) < 100]))
        #plt.colorbar()
        #plt.show()


        #plt.hist(diff_u[np.abs(diff) < 100], 50)
        #plt.title('MAD u: ' + str(np.round(MAD_u,2)))
        #plt.xlim([-100, 100])
        #plt.show()

        plt.hist(diff[diff < 10]*0.002, 50)
        #plt.title('MAD v: ' + str(np.round(MAD_v, 2)))
        #plt.xlim([-100, 100])
        plt.show()
        #
        self.diff = diff



    def map_pixels_back_to_datacube(self, w_datacube):
        """The projected formats can be transformed back with four integer transforms and interpolated accordingly"""
        """As a simple strategy we perform bilinear interpolation"""

        indexes_grid = np.flip(self.indexes.reshape((self.height_rectified, self.width_rectified)), axis = 0)

        v = self.feature_uv_hsi[:, 0]
        u = self.feature_uv_hsi[:, 1]

        v_rgb = self.feature_uv_rgb[:, 0]
        u_rgb = self.feature_uv_rgb[:, 1]

        # Should transform rgb coordinates directly to world coordinates
        ## Conversion to global coordinates
        x = self.feature_uv_rgb[:, 0]
        y = self.feature_uv_rgb[:, 1]

        xoff, a, b, yoff, d, e = self.transform_pixel_projected


        xp = a * x + b * y + xoff + 0.5*a # The origin of the image coordinate system is located at 0.5,0.5
        yp = d * x + e * y + yoff + 0.5*e
        zp = np.zeros(yp.shape)
        for i in range(xp.shape[0]):
            temp = [x for x in self.raster_dem.sample([(xp[i], yp[i])])]
            zp[i] = float(temp[0])

        if self.is_global != True:
            self.features_points = np.concatenate((xp.reshape((-1,1)), yp.reshape((-1,1)), zp.reshape((-1,1))), axis = 1)
        else:
            geocsc = CRS.from_epsg(self.epsg_geocsc)
            proj = CRS.from_epsg(self.epsg_proj)
            transformer = Transformer.from_crs(proj, geocsc)
            self.features_points = np.zeros((xp.shape[0], 3))



            (xECEF, yECEF, zECEF) = transformer.transform(xx=xp, yy=yp, zz=zp)

            self.features_points[:, 0] = xECEF - self.offX
            self.features_points[:, 1] = yECEF - self.offY
            self.features_points[:, 2] = zECEF - self.offZ




        #

        self.v_datacube_hsi = np.zeros((v.shape[0], 4))
        self.u_datacube_hsi = np.zeros((v.shape[0], 4))

        # See wikipedia
        for i in range(4):
            if i == 0:
                u1 = np.floor(u).astype(np.int32)  # row
                v1 = np.floor(v).astype(np.int32)  # col
            elif i == 1:
                u1 = np.floor(u).astype(np.int32)  # row
                v1 = np.ceil(v).astype(np.int32)  # col
            elif i == 2:
                u1 = np.ceil(u).astype(np.int32)  # row
                v1 = np.floor(v).astype(np.int32)  # col
            else:
                u1 = np.ceil(u).astype(np.int32)  # row
                v1 = np.ceil(v).astype(np.int32)  # col

            ind_datacube_hsi = indexes_grid[u1, v1] # 1D Indexer til rå datakube

            self.v_datacube_hsi[:, i] = ind_datacube_hsi % w_datacube
            self.u_datacube_hsi[:, i]  = (ind_datacube_hsi - self.v_datacube_hsi[:, i]) / w_datacube


        self.x1_x_hsi = v - np.floor(v)
        self.y1_y_hsi = u - np.floor(u)

        #self.x1_x_rgb = v_rgb - np.floor(v_rgb)
        #self.y1_y_rgb = u_rgb - np.floor(u_rgb)

def dem_2_mesh(path_dem, model_path, config):
    """
    A function for converting a specified DEM to a 3D mesh model (*.vtk, *.ply or *.stl). Consequently, mesh should be thought of as 2.5D representation.
    :param path_dem: string
    path to dem for reading
    :param model_path: string
    path to where 3D mesh model is to be written.
    :return: Nothing
    """
    # Input and output file paths

    output_xyz = model_path.split(sep = '.')[0] + '.xyz'
    # No-data value
    #no_data_value = int(config['General']['nodataDEM'])  # Replace with your actual no-data value
    # Open the input raster dataset
    ds = gdal.Open(path_dem)

    # 

    if ds is None:
        print(f"Failed to open {path_dem}")
    else:
        # Read the first band (band index is 1)
        band = ds.GetRasterBand(1)
        no_data_value = band.GetNoDataValue()
        if band is None:
            print(f"Failed to open band 1 of {path_dem}")
        else:
            # Get the geotransform information to calculate coordinates
            geotransform = ds.GetGeoTransform()
            x_origin = geotransform[0]
            y_origin = geotransform[3]
            x_resolution = geotransform[1]
            y_resolution = geotransform[5]
            # Get the CRS information
            spatial_reference = osr.SpatialReference(ds.GetProjection())

            # Get the EPSG code
            epsg_proj = None
            if spatial_reference.IsProjected():
                epsg_proj = spatial_reference.GetAttrValue("AUTHORITY", 1)
            elif spatial_reference.IsGeographic():
                epsg_proj = spatial_reference.GetAttrValue("AUTHORITY", 0)

            print(f"DEM projected EPSG Code: {epsg_proj}")

            config.set('Coordinate Reference Systems', 'dem_epsg', str(epsg_proj))
            
            # Get the band's data as a NumPy array
            band_data = band.ReadAsArray()
            # Create a mask to identify no-data values
            mask = band_data != no_data_value
            # Create and open the output XYZ file for writing if it does not exist:
            #if not os.path.exists(output_xyz):
            with open(output_xyz, 'w') as xyz_file:
                # Write data to the XYZ file using the mask and calculated coordinates
                for y in range(ds.RasterYSize):
                    for x in range(ds.RasterXSize):
                        if mask[y, x]:
                            x_coord = x_origin + x * x_resolution
                            y_coord = y_origin + y * y_resolution
                            xyz_file.write(f"{x_coord} {y_coord} {band_data[y, x]}\n")
            # Clean up
            ds = None
            band = None
    print("Conversion completed.")
    points = np.loadtxt(output_xyz)
    # Create a pyvista point cloud object
    cloud = pv.PolyData(points)
    # Generate a mesh from
    mesh = cloud.delaunay_2d()

    epsg_geocsc = config['Coordinate Reference Systems']['geocsc_epsg_export']
    # Transform the mesh points to from projected to geocentric ECEF.
    geocsc = CRS.from_epsg(epsg_geocsc)
    proj = CRS.from_epsg(epsg_proj)
    transformer = Transformer.from_crs(proj, geocsc)

    print(f"Mesh geocentric EPSG Code: {epsg_geocsc}")

    points_proj = mesh.points

    eastUTM = points_proj[:, 0].reshape((-1, 1))
    northUTM = points_proj[:, 1].reshape((-1, 1))
    heiUTM = points_proj[:, 2].reshape((-1, 1))

    (xECEF, yECEF, zECEF) = transformer.transform(xx=eastUTM, yy=northUTM, zz=heiUTM)

    mesh.points[:, 0] = xECEF.reshape(-1)
    mesh.points[:, 1] = yECEF.reshape(-1)
    mesh.points[:, 2] = zECEF.reshape(-1)

    #mean_vec = np.mean(mesh.points, axis = 0)

    offX = float(config['General']['offsetX'])
    offY = float(config['General']['offsetY'])
    offZ = float(config['General']['offsetZ'])

    pos0 = np.array([offX, offY, offZ]).reshape((1, -1))

    mesh.points -= pos0 # Add appropriate offset
    # Save mesh
    mesh.save(model_path)


def position_transform_ecef_2_llh(position_ecef, epsg_from, epsg_to, config):
    """
    Function for transforming ECEF positions to latitude longitude height
    :param position_ecef: numpy array floats (n,3)
    :param epsg_from: int
    EPSG code of the original geocentric coordinate system (ECEF)
    :param epsg_to: int
    EPSG code of the transformed geodetic coordinate system
    :return lat_lon_hei: numpy array floats (n,3)
    latitude, longitude ellipsoid height.
    """
    geocsc = CRS.from_epsg(epsg_from)
    geod = CRS.from_epsg(epsg_to)
    transformer = Transformer.from_crs(geocsc, geod)

    xECEF = position_ecef[:, 0].reshape((-1, 1))
    yECEF = position_ecef[:, 1].reshape((-1, 1))
    zECEF = position_ecef[:, 2].reshape((-1, 1))

    (lat, lon, hei) = transformer.transform(xx=xECEF, yy=yECEF, zz=zECEF)

    lat_lon_hei = np.zeros(position_ecef.shape)
    lat_lon_hei[:, 0] = lat.reshape((position_ecef.shape[0], 1))
    lat_lon_hei[:, 1] = lon.reshape((position_ecef.shape[0], 1))
    lat_lon_hei[:, 2] = hei.reshape((position_ecef.shape[0], 1))

    return lat_lon_hei








