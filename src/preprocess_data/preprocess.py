import os
import json
import pdb

import skimage
import ants
import warnings
import pandas as pd
import numpy as np
import SimpleITK as sitk

# Rich progres bar
from rich.console import Console
from rich.text import Text

from runtime.utils import (
    get_fg_mask_bbox,
    crop_to_fg,
    create_empty_dir,
    get_new_dims,
    ants_to_sitk,
    sitk_to_ants,
    aniso_intermediate_resample,
    check_anisotropic,
    make_onehot,
    get_progress_bar
)

console = Console()

"""
Functions for resampling images and masks
"""


def resample_image(img_ants, target_spacing, new_size=None):
    img_sitk = ants_to_sitk(img_ants)
    if new_size is None:
        new_size = get_new_dims(img_sitk, target_spacing)
    anisotropic, low_res_axis = check_anisotropic(img_sitk)

    if anisotropic:
        img_sitk = aniso_intermediate_resample(img_sitk, new_size, target_spacing, low_res_axis)

    img_sitk = sitk.Resample(img_sitk,
                             size=new_size,
                             transform=sitk.Transform(),
                             interpolator=sitk.sitkBSpline,
                             outputOrigin=img_sitk.GetOrigin(),
                             outputSpacing=target_spacing,
                             outputDirection=img_sitk.GetDirection(),
                             defaultPixelValue=0,
                             outputPixelType=img_sitk.GetPixelID())

    return sitk_to_ants(img_sitk)


def resample_mask(mask_ants, labels, target_spacing, new_size=None):
    # Get one hot encoded sitk series
    masks_sitk = make_onehot(mask_ants, labels)
    if new_size is None:
        new_size = get_new_dims(masks_sitk[0], target_spacing)
    anisotropic, low_res_axis = check_anisotropic(masks_sitk[0])

    for i in range(len(labels)):
        if anisotropic:
            masks_sitk[i] = aniso_intermediate_resample(masks_sitk[i], new_size, target_spacing, low_res_axis)

        # Use linear interpolation for masks
        masks_sitk[i] = sitk.Resample(masks_sitk[i],
                                      size=new_size,
                                      transform=sitk.Transform(),
                                      interpolator=sitk.sitkLinear,
                                      outputOrigin=masks_sitk[i].GetOrigin(),
                                      outputSpacing=target_spacing,
                                      outputDirection=masks_sitk[i].GetDirection(),
                                      defaultPixelValue=0,
                                      outputPixelType=masks_sitk[i].GetPixelID())

    mask = sitk_to_ants(sitk.JoinSeries(masks_sitk))
    mask = mask.numpy()
    mask = np.argmax(mask, axis=-1)

    mask = ants.from_numpy(data=mask.astype("float32"))
    mask.set_spacing(target_spacing)
    mask.set_origin(mask_ants.origin)
    mask.set_direction(mask_ants.direction)

    return mask


"""
Normalization functions
"""


def window_and_normalize(image, config):
    if config["use_nz_mask"]:
        nzmask = (image != 0).astype("float32")
        nonzeros = image[nzmask != 0]

    if config["modality"] == "ct":
        # Window image
        lower = config["window_range"][0]
        upper = config["window_range"][1]
        image = np.clip(image, lower, upper)

        # Normalize image
        mean = config["global_z_score_mean"]
        std = config["global_z_score_std"]
        image = (image - mean) / std
        if config["use_nz_mask"]:
            image *= nzmask
    else:
        if config["use_nz_mask"]:
            # Window image
            lower = np.percentile(nonzeros, 0.5)
            upper = np.percentile(nonzeros, 99.5)
            image = np.clip(image, lower, upper)

            # Normalize image
            mean = np.mean(nonzeros)
            std = np.std(nonzeros)
            image = (image - mean) / std
            image *= nzmask
        else:
            # Window image
            lower = np.percentile(image, 0.5)
            upper = np.percentile(image, 99.5)
            image = np.clip(image, lower, upper)

            # Normalize image
            mean = np.mean(image)
            std = np.std(image)
            image = (image - mean) / std

    return image


def preprocess_example(config, image_list, mask, fg_bbox=None):
    training = True
    if mask is None:
        training = False

    # Read all images (and mask if training)
    images = list()
    for i, image_path in enumerate(image_list):
        # Load image as ants image
        image = ants.image_read(image_path)

        # Get foreground mask if necessary
        if i == 0 and config["crop_to_fg"] and fg_bbox is None:
            fg_bbox = get_fg_mask_bbox(image)

        if config["crop_to_fg"]:
            # Only compute foreground mask once
            image = crop_to_fg(image, fg_bbox)

        # N4 bias correction
        if config["use_n4_bias_correction"]:
            image = ants.n4_bias_field_correction(image)

        # Put all images into standard space
        image = ants.reorient_image2(image, "RAI")
        image.set_direction(np.eye(3))
        if not np.array_equal(image.spacing, config["target_spacing"]):
            image = resample_image(image, target_spacing=config["target_spacing"])

        images.append(image)

    if training:
        # Read mask if we are in training mode
        mask = ants.image_read(mask)

        # Crop to foreground
        if config["crop_to_fg"]:
            mask = crop_to_fg(mask, fg_bbox)

        # Put mask into standard space
        mask = ants.reorient_image2(mask, "RAI")
        mask.set_direction(np.eye(3))
        if not np.array_equal(mask.spacing, config["target_spacing"]):
            mask = resample_mask(mask, labels=config["labels"], target_spacing=config["target_spacing"])

        # Add channel axis to mask
        mask = np.expand_dims(mask.numpy(), axis=-1)
    else:
        mask = None

    # Apply windowing and normalization to images
    # Get dimensions of image in standard space
    image = np.zeros((*images[0].shape, len(image_list)))
    for i in range(len(image_list)):
        img = images[i].numpy()
        img = window_and_normalize(img, config)

        image[..., i] = img

    return image, mask, fg_bbox


def convert_nifti_to_numpy(image_list, mask):
    dims = ants.image_header_info(image_list[0])
    dims = dims["dimensions"]

    # Convert images
    image_npy = np.zeros((*dims, len(image_list)))
    for i, image_path in enumerate(image_list):
        image = ants.image_read(image_path)
        image_npy[..., i] = image.numpy()

    # Convert mask if given
    if mask is not None:
        mask_npy = ants.image_read(mask)
        mask_npy = np.expand_dims(mask_npy.numpy(), axis=-1)
    else:
        mask_npy = None

    # Don't return a fg bounding box
    fg_bbox = None

    return image_npy, mask_npy, fg_bbox


def preprocess_dataset(args):
    # Get configuration file
    config_file = os.path.join(args.results, "config.json")

    with open(config_file, "r") as file:
        config = json.load(file)

    if config["modality"] != "mr" and config["use_n4_bias_correction"]:
        warnings.warn("N4 bias correction should not be used for modality {}".format(config["modality"]))

    # Get paths to dataset
    df = pd.read_csv(os.path.join(args.results, "train_paths.csv"))

    # Create output directories if they do not exist
    images_dir = os.path.join(args.numpy, "images")
    create_empty_dir(images_dir)

    labels_dir = os.path.join(args.numpy, "labels")
    create_empty_dir(labels_dir)

    text = Text("\nPreprocessing dataset\n")
    text.stylize("bold")
    console.print(text)

    if args.no_preprocess:
        progress = get_progress_bar("Converting nifti to npy")
    else:
        progress = get_progress_bar("Preprocessing")

    with progress as pb:
        for i in pb.track(range(len(df))):
            # Get paths to images for single patient
            patient = df.iloc[i].to_dict()

            # Get list of image paths and segmentation mask
            image_list = list(patient.values())[3:len(patient)]
            mask = patient["mask"]

            # If already given preprocessed data, then just convert it to numpy data.
            # Otherwise, run preprocessing
            if args.no_preprocess:
                image_npy, mask_npy, _ = convert_nifti_to_numpy(image_list, mask)
            else:
                if config["crop_to_fg"]:
                    fg_bboxes = pd.read_csv(os.path.join(args.results, "fg_bboxes.csv"))

                fg_bbox = fg_bboxes.iloc[i].to_dict()
                image_npy, mask_npy, _ = preprocess_example(config, image_list, mask, fg_bbox)

    with open(config_file, "w") as outfile:
        json.dump(config, outfile, indent=2)