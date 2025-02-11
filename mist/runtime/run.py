"""Training class for MIST."""
import os
from typing import Optional

import numpy as np
import pandas as pd
import rich
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from sklearn.model_selection import train_test_split
from torch import nn
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.tensorboard import SummaryWriter
from monai.inferers import sliding_window_inference

# Import MIST modules.
from mist.data_loading import dali_loader
from mist.models import get_model
from mist.runtime import exceptions
from mist.runtime import loss_functions
from mist.runtime import progress_bar
from mist.runtime import utils

# Define console for rich.
console = rich.console.Console()


class Trainer:
    """Training class for MIST.

    Attributes:
        mist_arguments: User defined arguments for MIST.
        file_paths: Paths to files like dataset description, config file, and
            model configuration file.
        data_structures: Data structures like dataset description and
            configuration data.
        class_weights: Class weights for weighted loss functions.
        boundary_loss_weighting_schedule: Weighting schedule for boundary loss
            functions.
        fixed_loss_functions: Loss functions for validation and VAE loss.
    """

    def __init__(self, mist_arguments):
        """Initialize the trainer class."""
        # Store user arguments.
        self.mist_arguments = mist_arguments

        # Initialize data paths dictionary. This dictionary contains paths to
        # files like the dataset description, MIST configuration, model
        # configuration, and training paths dataframe.
        self._initialize_file_paths()

        # Initialize data structures. This function reads the dataset
        # description, MIST configuration, and training paths dataframe from the
        # corresponding files. These data structures are used during to set up
        # the training process.
        self._initialize_data_structures()

        # Set up model configuration. The model configuration saves parameters
        # like the model name, number of channels, number of classes, deep
        # supervision, deep supervision heads, pocket, patch size, target
        # spacing, VAE regularization, and use of residual blocks. We use these
        # parameters to build the model during training and for inference.
        self._create_model_configuration()

        # Set class weights.
        self.class_weights = (
            self.data_structures["mist_configuration"]["class_weights"]
            if self.mist_arguments.use_config_class_weights else None
        )

        # Initialize boundary loss weighting schedule.
        self.boundary_loss_weighting_schedule = utils.AlphaSchedule(
            n_epochs=self.mist_arguments.epochs,
            schedule=self.mist_arguments.boundary_loss_schedule,
            constant=self.mist_arguments.loss_schedule_constant,
            init_pause=self.mist_arguments.linear_schedule_pause,
            step_length=self.mist_arguments.step_schedule_step_length
        )

        # Initialize fixed loss functions.
        self.fixed_loss_functions = {
            "validation": loss_functions.DiceCELoss(),
            "vae": loss_functions.VAELoss(),
        }

    def _initialize_file_paths(self):
        """Initialize and store necessary file paths."""
        self.file_paths = {
            "dataset_description": self.mist_arguments.data,
            "mist_configuration": os.path.join(
                self.mist_arguments.results, "config.json"
            ),
            "model_configuration": os.path.join(
                self.mist_arguments.results, "models", "model_config.json"
            ),
            "training_paths_dataframe": os.path.join(
                self.mist_arguments.results, "train_paths.csv"
            ),
        }

    def _initialize_data_structures(self):
        """Read and store data structures such as configuration and paths."""
        # Initialize data structures dictionary.
        self.data_structures = {}

        # Check if the corresponding files exist. We omit the model
        # configuration file since it does not exist yet. The model
        # configuration will be created later.
        for file_path in (
            path for key, path in self.file_paths.items()
            if key != "model_configuration"
        ):
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"File not found: {file_path}")

        # Read the dataset description file.
        self.data_structures["dataset_description"] = utils.read_json_file(
            self.file_paths["dataset_description"]
        )

        # Read the MIST configuration file.
        self.data_structures["mist_configuration"] = utils.read_json_file(
            self.file_paths["mist_configuration"]
        )

        # Read the training paths dataframe.
        self.data_structures["training_paths_dataframe"] = pd.read_csv(
            self.file_paths["training_paths_dataframe"]
        )

    def _create_model_configuration(self):
        """Create model configuration.

        This function creates the model configuration based on the user
        arguments. This will either create a new model configuration or
        read an existing model configuration from a file (i.e., pretrained).
        """
        # Get the number of channels and classes from the dataset description.
        number_of_channels = len(
            self.data_structures["dataset_description"]["images"]
        )
        number_of_classes = len(
            self.data_structures["dataset_description"]["labels"]
        )

        if self.mist_arguments.model != "pretrained":
            # If the model is not pretrained, create a new model configuration.
            # Update the patch size if the user overrides it.
            if self.mist_arguments.patch_size is not None:
                self.data_structures["mist_configuration"]["patch_size"] = (
                    self.mist_arguments.patch_size
                )

            # Create a new model configuration based on user arguments.
            self.data_structures["model_configuration"] = {
                "model_name": self.mist_arguments.model,
                "n_channels": number_of_channels,
                "n_classes": number_of_classes,
                "deep_supervision": self.mist_arguments.deep_supervision,
                "deep_supervision_heads": (
                    self.mist_arguments.deep_supervision_heads
                ),
                "pocket": self.mist_arguments.pocket,
                "patch_size": (
                    self.data_structures["mist_configuration"]["patch_size"]
                ),
                "target_spacing": (
                    self.data_structures["mist_configuration"]["target_spacing"]
                ),
                "vae_reg": self.mist_arguments.vae_reg,
                "use_res_block": self.mist_arguments.use_res_block,
            }
        else:
            # If the model is pretrained, read the model configuration from the
            # pretrained model configuration file.
            # Path to the pretrained model configuration file.
            pretrained_model_config_path = os.path.join(
                self.mist_arguments.pretrained_model_path, "model_config.json"
            )

            # Check if the pretrained model configuration file exists.
            if not os.path.exists(pretrained_model_config_path):
                raise FileNotFoundError(
                    f"Pretrained model configuration file not found: "
                    f"{pretrained_model_config_path}"
                )

            # Load the pretrained model configuration from file.
            self.data_structures["model_configuration"] = utils.read_json_file(
                pretrained_model_config_path
            )

            # Update the number of channels and classes from the current
            # dataset description.
            self.data_structures["model_configuration"].update(
                {
                    "n_channels": number_of_channels,
                    "n_classes": number_of_classes,
                }
            )

            # Update the patch size in the MIST configuration based on the
            # patch size from the pretrained model configuration.
            self.data_structures["mist_configuration"]["patch_size"] = (
                self.data_structures["model_configuration"]["patch_size"]
            )

        # Save the model configuration to file.
        utils.write_json_file(
            self.file_paths["model_configuration"],
            self.data_structures["model_configuration"],
        )

    # Set up for distributed training
    def setup(self, rank: int, world_size: int) -> None:
        """Set up for distributed training.

        Args:
            rank: Rank of the process.
            world_size: Number of processes.
        """
        os.environ["MASTER_ADDR"] = "localhost"
        os.environ["MASTER_PORT"] = self.mist_arguments.master_port
        dist.init_process_group("nccl", rank=rank, world_size=world_size)

    # Clean up processes after distributed training
    def cleanup(self):
        """Clean up processes after distributed training."""
        dist.destroy_process_group()

    def train(self, rank: int, world_size: int) -> None:
        """Train the model.

        Args:
            rank: Rank of the process.
            world_size: Number of processes
        """
        # Set up for distributed training.
        self.setup(rank, world_size)

        # Set device rank for each process.
        torch.cuda.set_device(rank)

        # Display the start of training message.
        if rank == 0:
            text = rich.text.Text("\nStarting training\n")
            text.stylize("bold")
            console.print(text)

        # Start training for each fold.
        for fold in self.mist_arguments.folds:
            # Get training ids from dataframe.
            train_ids = list(
                self.data_structures["training_paths_dataframe"].loc[
                    self.data_structures["training_paths_dataframe"]["fold"]
                    != fold
                ]["id"]
            )

            # Get list of training images and labels.
            train_images = utils.get_numpy_file_paths_list(
                base_dir=self.mist_arguments.numpy,
                folder="images",
                patient_ids=train_ids,
            )
            train_labels = utils.get_numpy_file_paths_list(
                base_dir=self.mist_arguments.numpy,
                folder="labels",
                patient_ids=train_ids,
            )
            if self.mist_arguments.use_dtms:
                # Get list of training distance transform maps.
                train_dtms = utils.get_numpy_file_paths_list(
                    base_dir=self.mist_arguments.numpy,
                    folder="dtms",
                    patient_ids=train_ids,
                )

                # Split into training and validation sets with distance
                # transform maps.
                train_images, val_images, train_labels_dtms, val_labels_dtms = train_test_split(
                    train_images,
                    list(zip(train_labels, train_dtms)),
                    test_size=self.mist_arguments.val_percent,
                    random_state=self.mist_arguments.seed_val,
                )

                # Unpack labels and distance transform maps.
                train_labels, train_dtms = zip(*train_labels_dtms)
                val_labels, _ = zip(*val_labels_dtms)
            else:
                # Split data into training and validation sets without DTMs
                train_images, val_images, train_labels, val_labels = train_test_split(
                    train_images,
                    train_labels,
                    test_size=self.mist_arguments.val_percent,
                    random_state=self.mist_arguments.seed_val
                )

                # No DTMs in this case.
                train_dtms = None

            # The number of validation images must be greater than or equal to
            # the number of GPUs used for training.
            if len(val_images) < world_size:
                raise exceptions.InsufficientValidationSetError(
                    val_size=len(val_images), world_size=world_size
                )

            # Get number of validation steps. This is the number of validation
            # images divided by the number of GPUs (i.e., the world size).
            val_steps = len(val_images) // world_size

            # Get training data loader.
            train_loader = dali_loader.get_training_dataset(
                imgs=train_images,
                lbls=train_labels,
                dtms=train_dtms,
                batch_size=self.mist_arguments.batch_size // world_size,
                oversampling=self.mist_arguments.oversampling,
                patch_size=(
                    self.data_structures["mist_configuration"]["patch_size"]
                ),
                seed=self.mist_arguments.seed_val,
                num_workers=self.mist_arguments.num_workers,
                rank=rank,
                world_size=world_size,
            )

            # Get validation data loader.
            validation_loader = dali_loader.get_validation_dataset(
                imgs=val_images,
                lbls=val_labels,
                seed=self.mist_arguments.seed_val,
                num_workers=self.mist_arguments.num_workers,
                rank=rank,
                world_size=world_size
            )

            # Get steps per epoch if not given by user
            if self.mist_arguments.steps_per_epoch is None:
                self.mist_arguments.steps_per_epoch = (
                    len(train_images) // self.mist_arguments.batch_size
                )
            else:
                self.mist_arguments.steps_per_epoch = (
                    self.mist_arguments.steps_per_epoch
                )

            # Get loss function
            loss_fn = loss_functions.get_loss(
                self.mist_arguments, class_weights=self.class_weights
            )

            # Make sure we are using/have DTMs for boundary-based loss
            # functions.
            if self.mist_arguments.loss in ["bl", "hdl", "gsl"]:
                if not self.mist_arguments.use_dtms:
                    raise ValueError(
                        f"For loss function '{self.mist_arguments.loss}', the "
                        "--use-dtms flag must be enabled."
                    )

                if isinstance(train_dtms, list):
                    # Check if the number of training images, labels, and
                    # distance transforms match. If not, raise an error.
                    if not(
                        len(train_images) == len(train_labels) == len(
                            train_dtms
                        )
                    ):
                        raise ValueError(
                            "Mismatch in the number of training images, "
                            "labels, and distance transforms. Ensure that the "
                            "number of distance transforms matches the number "
                            "of training images and labels. Found "
                            f"{len(train_images)} training images, "
                            f"{len(train_labels)} training labels, and "
                            f"{len(train_dtms)} training distance transforms."
                        )

            # Define the model from the model configuration file.
            if self.mist_arguments.model != "pretrained":
                # Create new model from the model configuration.
                model = get_model.get_model(
                    **self.data_structures["model_configuration"]
                )
            else:
                model = get_model.configure_pretrained_model(
                    self.mist_arguments.pretrained_model_path,
                    self.data_structures["model_configuration"]["n_channels"],
                    self.data_structures["model_configuration"]["n_classes"],
                )

            # Make batch normalization compatible with DDP.
            model = nn.SyncBatchNorm.convert_sync_batchnorm(model)

            # Set up model for distributed data parallel training.
            model.to(rank)
            if self.mist_arguments.model != "pretrained":
                model = DDP(model, device_ids=[rank])
            else:
                # This seems to work with pretrained models. We will need to
                # test this further.
                model = DDP(
                    model, device_ids=[rank], find_unused_parameters=True
                )

            # Get optimizer and lr scheduler
            optimizer = utils.get_optimizer(self.mist_arguments, model)
            learning_rate_scheduler = utils.get_lr_schedule(
                self.mist_arguments, optimizer
            )

            # Float16 inputs during the forward pass produce float16 gradients
            # in the backward pass. Small gradient values may underflow to zero,
            # causing updates to corresponding parameters to be lost. To prevent
            # this, "gradient scaling" multiplies the loss by a scale factor
            # before the backward pass, increasing gradient magnitudes to avoid
            # underflow. Gradients must be unscaled before the optimizer updates
            # the parameters to ensure the learning rate is unaffected.
            if self.mist_arguments.amp:
                amp_gradient_scaler = torch.amp.GradScaler("cuda")

            # Only log metrics on first process (i.e., rank 0).
            if rank == 0:
                # Compute running averages for training and validation losses.
                running_loss_train = utils.RunningMean()
                running_loss_validation = utils.RunningMean()

                # Initialize best validation loss to infinity.
                best_validation_loss = np.Inf

                # Set up tensorboard summary writer.
                writer = SummaryWriter(
                    os.path.join(
                        self.mist_arguments.results, "logs", f"fold_{fold}"
                        )
                    )

                # Path and name for best model for this fold.
                best_model_name = os.path.join(
                    self.mist_arguments.results, "models", f"fold_{fold}.pt"
                )

            def train_step(
                    image: torch.Tensor,
                    label: torch.Tensor,
                    dtm: Optional[torch.Tensor],
                    alpha: Optional[float],
            ) -> torch.Tensor:
                """Perform a single training step.

                Args:
                    image: Input image.
                    label: Ground truth label.
                    dtm: Distance transform map.
                    alpha: Weighting factor for boundary-based loss functions.

                Returns:
                    loss: Loss value for the batch.
                """
                # Compute loss for the batch.
                def compute_loss() -> torch.Tensor:
                    """Compute loss for the batch.

                    Args:
                        None

                    Returns:
                        loss: Loss value for the batch.
                    """
                    # Make predictions for the batch.
                    output = model(image)

                    # Compute loss for the batch. The inputs to the loss
                    # function depend on the loss function being used.
                    if self.mist_arguments.use_dtms:
                        # Use distance transform maps for boundary-based loss
                        # functions.
                        loss = loss_fn(label, output["prediction"], dtm, alpha)
                    elif self.mist_arguments.loss in ["cldice"]:
                        # Use the alpha parameter to weight the cldice and
                        # dice with cross entropy loss functions.
                        loss = loss_fn(label, output["prediction"], alpha)
                    else:
                        # Use only the image and label for other loss functions
                        # like dice with cross entropy.
                        loss = loss_fn(label, output["prediction"])

                    # If deep supervision is enabled, compute the additional
                    # losses from the deep supervision heads. Deep supervision
                    # provides additional output layers that guide the model
                    # during training by supplying intermediate supervision
                    # signals at various stages of the model.

                    # We scale the loss from each deep supervision head by a
                    # factor of (0.5 ** (k + 1)), where k is the index of the
                    # deep supervision head. This creates a geometric series
                    # that gives decreasing weight to deeper (later) supervision
                    # heads. The idea is to ensure that the loss from earlier
                    # heads (closer to the final output) contributes more to the
                    # total loss, while still incorporating the information from
                    # later heads.

                    # After summing the losses from all deep supervision heads,
                    # we normalize the total loss using a correction factor
                    # (c_norm). This factor is derived from the sum of the
                    # geometric series (1 / (2 - 2 ** -(n+1))), where n is the
                    # number of deep supervision heads. The normalization
                    # ensures that the total loss isn't biased or dominated by
                    # the deep supervision losses.
                    if self.mist_arguments.deep_supervision:
                        for k, p in enumerate(output["deep_supervision"]):
                            # Apply the loss function based on the model's
                            # configuration. If distance transform maps
                            # are used, pass them to the loss function.
                            if self.mist_arguments.use_dtms:
                                loss += 0.5 ** (k + 1) * loss_fn(
                                    label, p, dtm, alpha
                                )
                            # If cldice loss is used, pass alpha to the loss
                            # function.
                            elif self.mist_arguments.loss in ["cldice"]:
                                loss += 0.5 ** (k + 1) * loss_fn(
                                    label, p, alpha
                                )
                            # Otherwise, compute the loss normally.
                            else:
                                loss += 0.5 ** (k + 1) * loss_fn(label, p)

                        # Normalize the total loss from deep supervision heads
                        # using a correction factor to prevent it from
                        # dominating the main loss.
                        c_norm = 1 / (2 - 2 ** -(
                            len(output["deep_supervision"]) + 1
                            )
                        )
                        loss *= c_norm

                    # Check if Variational Autoencoder (VAE) regularization
                    # is enabled. VAE regularization encourages the model to
                    # learn a latent space that follows a normal
                    # distribution, which helps the model generalize better.
                    # This term adds a penalty to the loss, based on how much
                    # the learned latent space deviates from the expected
                    # distribution (usually Gaussian). We then sample from this
                    # latent space to reconstruct the input image. The total VAE
                    # loss is the sum of the Kullback-Leibler (KL) divergence
                    # and the reconstruction loss.
                    if self.mist_arguments.vae_reg:
                        vae_loss = self.fixed_loss_functions["vae"](
                            image, output["vae_reg"]
                        )
                        # Multiply the computed VAE loss by a scaling
                        # factor, vae_penalty, which controls the strength of
                        # the regularization.
                        loss += self.mist_arguments.vae_penalty * vae_loss


                    # L2 regularization term. This term adds a penalty to the
                    # loss based on the L2 norm of the model's parameters.
                    if self.mist_arguments.l2_reg:
                        l2_norm_of_model_parameters = 0.0
                        for param in model.parameters():
                            l2_norm_of_model_parameters += (
                                torch.norm(param, p=2)
                            )

                        # Update the loss with the L2 regularization term scaled
                        # by the l2_penalty parameter.
                        loss += (
                            self.mist_arguments.l2_penalty *
                            l2_norm_of_model_parameters
                        )

                    # L1 regularization term. This term adds a penalty to the
                    # loss based on the L1 norm of the model's parameters.
                    if self.mist_arguments.l1_reg:
                        l1_norm_of_model_parameters = 0.0
                        for param in model.parameters():
                            l1_norm_of_model_parameters += (
                                torch.norm(param, p=1)
                            )

                        # Update the loss with the L1 regularization term scaled
                        # by the l1_penalty parameter.
                        loss += (
                            self.mist_arguments.l1_penalty *
                            l1_norm_of_model_parameters
                        )
                    return loss

                # Zero out the gradients from the previous batch.
                # Gradients accumulate by default in PyTorch, so it's important
                # to reset them at the start of each training iteration to avoid
                # interference from prior batches.
                optimizer.zero_grad()

                # Check if automatic mixed precision (AMP) is enabled for this
                # training step.
                if self.mist_arguments.amp:
                    # AMP is used to speed up training and reduce memory usage
                    # by performing certain operations in lower precision
                    # (e.g., float16). This can improve the efficiency of
                    # training on GPUs without significant loss in accuracy.

                    # Use `torch.autocast` to automatically handle mixed
                    # precision operations on the GPU. This context manager
                    # ensures that certain operations are performed in float16
                    # precision while others remain in float32, depending
                    # on what is most efficient and appropriate.
                    with torch.autocast(
                        device_type="cuda", dtype=torch.float16
                    ):
                        # Perform the forward pass and compute the loss using
                        # mixed precision.
                        loss = compute_loss()

                    # Backward pass: Compute gradients by scaling the loss to
                    # prevent underflow. Scaling is necessary when using AMP
                    # because very small gradients in float16 could underflow
                    # (become zero) during training. The scaler multiplies the
                    # loss by a large factor before computing the gradients to
                    # mitigate underflow.
                    amp_gradient_scaler.scale(loss).backward()

                    # If gradient clipping is enabled, apply it after unscaling
                    # the gradients. Gradient clipping prevents exploding
                    # gradients by limiting the magnitude of the gradients to a
                    # specified maximum value (clip_norm_max).
                    if self.mist_arguments.clip_norm:
                        # Unscale the gradients before clipping, as they were
                        # previously scaled.
                        amp_gradient_scaler.unscale_(optimizer)

                        # Clip gradients to the maximum norm (clip_norm_max) to
                        # stabilize training.
                        torch.nn.utils.clip_grad_norm_(
                            model.parameters(),
                            self.mist_arguments.clip_norm_max
                        )

                    # Perform the optimizer step to update the model parameters.
                    # This step adjusts the model's weights based on the
                    # computed gradients.
                    amp_gradient_scaler.step(optimizer)

                    # Update the scaler after each iteration. This adjusts the
                    # scale factor used to prevent underflows or overflows in
                    # the future. The scaler increases or decreases the scaling
                    # factor dynamically based on whether gradients overflow.
                    amp_gradient_scaler.update()
                else:
                    # If AMP is not enabled, perform the forward pass and
                    # compute the loss using float32 precision.
                    loss = compute_loss()

                    # Compute the loss and its gradients.
                    loss.backward()

                    # Apply gradient clipping if enabled.
                    if self.mist_arguments.clip_norm:
                        torch.nn.utils.clip_grad_norm_(
                            model.parameters(),
                            self.mist_arguments.clip_norm_max
                        )

                    # Perform the optimizer step to update the model parameters.
                    optimizer.step()
                return loss

            def val_step(
                    image: torch.Tensor,
                    label: torch.Tensor
            ) -> torch.Tensor:
                """Perform a single validation step.

                Args:
                    image: Input image.
                    label: Ground truth label.

                Returns:
                    loss: Loss value for the batch.
                """
                pred = sliding_window_inference(
                    image,
                    roi_size=(
                        self.data_structures["mist_configuration"][
                            "patch_size"
                        ]
                    ),
                    overlap=self.mist_arguments.val_sw_overlap,
                    sw_batch_size=1,
                    predictor=model,
                    device=torch.device("cuda")
                )

                return self.fixed_loss_functions["validation"](label, pred)

            patch_counter=0

            # Train the model for the specified number of epochs.
            for epoch in range(self.mist_arguments.epochs):
                # Make sure gradient tracking is on, and do a pass over the
                # training data.
                model.train(True)

                # Only log metrics on first process (i.e., rank 0).
                if rank == 0:
                    ## with progress_bar.TrainProgressBar(
                    ##     epoch + 1,
                    ##     fold,
                    ##     self.mist_arguments.epochs,
                    ##     self.mist_arguments.steps_per_epoch
                    ## ) as pb:
                        for _ in range(self.mist_arguments.steps_per_epoch):
                            if patch_counter >=50:
                                break
                            # Get data from training loader.
                            data = train_loader.next()[0] #comment out to run with and without augmentation
                            mylbl = data["label"].detach().cpu().numpy()
                            myimg = data["image"].detach().cpu().numpy()
                            print("myimg.shape", myimg.shape)
                            import ants
                            ants.image_write(ants.from_numpy(myimg[patch_counter % myimg.shape[0], 0, :, :, :]), f'myimage{patch_counter:02}_00.nii.gz')
                            #ants.image_write(ants.from_numpy(myimg[patch_counter % myimg.shape[0], 1, :, :, :]), f'myimage{patch_counter:02}_01.nii.gz')
                            #ants.image_write(ants.from_numpy(mylbl[patch_counter % mylbl.shape[0], 0, :, :, :]), f'mylabel{patch_counter:02}_0.nii.gz') 
                            #ants.image_write(ants.from_numpy(myimg[0,0,:,:,:]), 'myimage00.nii.gz')
                            #ants.image_write(ants.from_numpy(myimg[0,1,:,:,:]), 'myimage01.nii.gz')
                            #ants.image_write(ants.from_numpy(myimg[1,0,:,:,:]), 'myimage10.nii.gz')
                            #ants.image_write(ants.from_numpy(myimg[1,1,:,:,:]), 'myimage11.nii.gz')
                            #ants.image_write(ants.from_numpy(mylbl[0,0,:,:,:]), 'mylabel0.nii.gz')
                            #ants.image_write(ants.from_numpy(mylbl[1,0,:,:,:]), 'mylabel1.nii.gz')
                            if myimg.shape[1] > 1:
                                ants.image_write(ants.from_numpy(myimg[patch_counter % myimg.shape[0], 1, :, :, :]), f'myimage{patch_counter:02}_01.nii.gz')
                                
                            ants.image_write(ants.from_numpy(mylbl[patch_counter % mylbl.shape[0], 0, :, :, :]), f'mylabel{patch_counter:02}_0.nii.gz')
                            patch_counter +=1 
                
                if patch_counter >= 50:
                    break 
                
            import ipdb; ipdb.set_trace()     

                            # Compute alpha for boundary loss functions. The
                            # alpha parameter is used to weight the boundary
                            # loss function with a region-based loss function
                            # like dice or cross entropy.
                            
        alpha = self.boundary_loss_weighting_schedule(epoch)
        if self.mist_arguments.use_dtms:
                                # Use distance transform maps for boundary-based
                                # loss functions. In this case, we pass them
                                # and the alpha parameter to the train_step.
                                image, label, dtm = (
                                    data["image"], data["label"], data["dtm"]
                                )

                                # Perform a single training step. Return
                                # the loss for the batch.
                                loss = train_step(image, label, dtm, alpha)
        else:
                                # If distance transform maps are not used, pass
                                # None for the dtm parameter. If we are using
                                # cldice loss, pass the alpha parameter to the
                                # train_step. Otherwise, pass None.
                                image, label = data["image"], data["label"]
                                if self.mist_arguments.loss in ["cldice"]:
                                    loss = train_step(image, label, None, alpha)
                                else:
                                    loss = train_step(image, label, None, None)

                            # Update update the learning rate scheduler.
        learning_rate_scheduler.step()

                            # Send all training losses to device 0 to add them.
        dist.reduce(loss, dst=0)

                            # Average the loss across all GPUs.
        current_loss = loss.item() / world_size

                            # Update the running loss for the progress bar.
        running_loss = running_loss_train(current_loss)

                            # Update the progress bar with the running loss.
        pb.update(loss=running_loss)
        #else:
                    # For all other processes, do not display the progress bar.
                    # Repeat the training steps shown above for the other GPUs.
        for _ in range(self.mist_arguments.steps_per_epoch):
                        # Get data from training loader.
                        data = train_loader.next()[0]
                        alpha = self.boundary_loss_weighting_schedule(epoch)
                        if self.mist_arguments.use_dtms:
                            image, label, dtm = (
                                data["image"], data["label"], data["dtm"]
                            )
                            loss = train_step(image, label, dtm, alpha)
                        else:
                            image, label = data["image"], data["label"]
                            if self.mist_arguments.loss in ["cldice"]:
                                loss = train_step(image, label, None, alpha)
                            else:
                                loss = train_step(image, label, None, None)

                        # Update the learning rate scheduler.
                        learning_rate_scheduler.step()

                        # Send the loss on the current GPU to device 0.
                        dist.reduce(loss, dst=0)

                # Wait for all processes to finish the epoch.
        dist.barrier()

                # Start validation. We don't need gradients on to do reporting.
        model.eval()
        with torch.no_grad():
                    # Only log metrics on first process (i.e., rank 0).
                    if rank == 0:
                        with progress_bar.ValidationProgressBar(
                            val_steps
                        ) as pb:
                            for _ in range(val_steps):
                                # Get data from validation loader.
                                data = validation_loader.next()[0]
                                image, label = data["image"], data["label"]

                                # Compute loss for single validation step.
                                val_loss = val_step(image, label)

                                # Send all validation losses to device 0 to add
                                # them.
                                dist.reduce(val_loss, dst=0)

                                # Average the loss across all GPUs.
                                current_val_loss = val_loss.item() / world_size

                                # Update the running loss for the progress bar.
                                running_val_loss = running_loss_validation(
                                    current_val_loss
                                )

                                # Update the progress bar with the running loss.
                                pb.update(loss=running_val_loss)

                        # Check if validation loss is lower than the current
                        # best validation loss. If so, save the model.
                        if running_val_loss < best_validation_loss:
                            text = rich.text.Text(
                                "Validation loss IMPROVED from "
                                f"{best_validation_loss:.4} "
                                f"to {running_val_loss:.4}\n"
                            )
                            text.stylize("bold")
                            console.print(text)

                            # Update the current best validation loss.
                            best_validation_loss = running_val_loss

                            # Save the model with the best validation loss.
                            torch.save(model.state_dict(), best_model_name)
                        else:
                            # Otherwise, log that the validation loss did not
                            # improve and display the best validation loss.
                            # Continue training with the current model.
                            text = rich.text.Text(
                                "Validation loss did NOT improve from "
                                f"{best_validation_loss:.4}\n"
                            )
                            console.print(text)
                    else:
                        # Repeat the validation steps for the other GPUs. Do
                        # not display the progress bar for these GPUs.
                        for _ in range(val_steps):
                            # Get data from validation loader.
                            data = validation_loader.next()[0]
                            image, label = data["image"], data["label"]

                            # Compute loss for single validation step.
                            val_loss = val_step(image, label)

                            # Send the loss on the current GPU to device 0.
                            dist.reduce(val_loss, dst=0)

                # Reset training and validation loaders after each epoch.
        train_loader.reset()
        validation_loader.reset()

                # Log the running loss for training and validation after each
                # epoch. Only log metrics on first process (i.e., rank 0).
        if rank == 0:
                    # Log the running loss for validation.
                    summary_data = {
                        "Training": running_loss,
                        "Validation": running_val_loss,
                    }
                    writer.add_scalars(
                        "Training vs. Validation Loss",
                        summary_data,
                        epoch + 1,
                    )
                    writer.flush()

                    # Reset running losses for new epoch.
                    running_loss_train.reset_states()
                    running_loss_validation.reset_states()

            # Wait for all processes to finish the fold.
        dist.barrier()

            # Close the tensorboard summary writer after each fold. Only
            # close the writer on the first process (i.e., rank 0).
        if rank == 0:
                writer.close()

        # Clean up processes after distributed training.
        self.cleanup()

    def fit(self):
        """Fit the model using multiprocessing.

        This function uses multiprocessing to train the model on multiple GPUs.
        It uses the `torch.multiprocessing.spawn` function to create multiple
        instances of the training function, each on a separate GPU.
        """
        # Train model
        world_size = torch.cuda.device_count()
        if world_size > 1:
            mp.spawn(
                self.train,
                args=(world_size,),
                nprocs=world_size,
                join=True,
            )
        # To enable pdb do not spawn multiprocessing for world_size = 1 
        else:
            self.train(0,world_size)
