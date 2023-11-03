# Standard python library
import configparser
import sys
import os

# Local resources
from scripts import georeference
from lib import parsing_utils
from scripts import visualize




from scripts.modulate_config import

# This script is meant to be used for testing the processing pipeline of airborne HI data

# The configuration file stores the settings for georeferencing
config_file = 'C:/Users/haavasl/PycharmProjects/hyperspectral_toolchain/data/NyAlesundAirborne28052023/configuration.ini'

# Set the data directory for the mission (locally where the data is stored)



# TODO: update config.ini automatically with paths for simple reproducability
config = configparser.ConfigParser()
config.read(config_file)

def main():
    ## Extract pose.csv and model.ply data from Agisoft Metashape (photogrammetry software) through API.
    ## Fails if you do not have an appropriate project.

    # The minimum for georeferencing is to parse 1) Mesh model and 2) The pose of the reference
    config = parsing_utils.export_pose(config_file)

    # TODO: replace "agisoft_export_model" with generic "export_model"
    #parsing_utils.agisoft_export_model(config_file)

    ## Visualize the data 3D photo model from RGB images and the time-resolved positions/orientations
    #visualize.show_mesh_camera(config)

    # Georeference the line scans of the hyperspectral imager. Utilizes parsed data
    # georeference.main(config_file, mode='georeference', is_calibrated=True)
    # Alternatively mode = 'calibrate'
    # georeference_mod.main(config_file, mode='calibrate', is_calibrated=True)


if __name__ == "__main__":
    main()