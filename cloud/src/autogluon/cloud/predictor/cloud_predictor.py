import os
import yaml
import tarfile
import logging

import pandas as pd
import boto3
import sagemaker
from datetime import datetime

from autogluon.common.loaders import load_pd
from autogluon.common.loaders import load_pkl
from autogluon.common.savers import save_pkl
from autogluon.common.utils.log_utils import set_logger_verbosity
from autogluon.common.utils.s3_utils import is_s3_url, s3_path_to_bucket_prefix
from autogluon.common.utils.utils import setup_outputdir

from ..data import FormatConverterFactory
from ..job import SageMakerFitJob, SageMakerBatchTransformationJob
from ..utils.ag_sagemaker import (
    AutoGluonSagemakerEstimator,
    AutoGluonSagemakerInferenceModel,
    AutoGluonRealtimePredictor,
    AutoGluonBatchPredictor
)
from ..utils.aws_utils import create_sagemaker_role_and_attach_policies
from ..utils.constants import SAGEMAKER_TRUST_REPLATIONSHIP, SAGEMAKER_POLICIES, VALID_ACCEPT
from ..utils.misc import MostRecentInsertedOrderedDict
from ..utils.script_paths import TRAIN_SCRIPT_PATH, TABULAR_SERVE_SCRIPT_PATH, TEXT_SERVE_SCRIPT_PATH
from ..utils.s3_utils import download_s3_file
from ..utils.sagemaker_utils import retrieve_available_framework_versions, retrieve_latest_framework_version
from ..utils.utils import unzip_file, rename_file_with_uuid

logger = logging.getLogger(__name__)


class CloudPredictor:

    predictor_file_name = 'CloudPredictor.pkl'

    def __init__(
        self,
        role_arn=None,
        local_output_path=None,
        cloud_output_path=None,
        verbosity=2
    ):
        """
        Parameters
        ----------
        role_arn: str
            The role_arn you want to use to grant cloud predictor necessary permission. 
            This role must have permission on AmazonS3FullAccess and AmazonSageMakerFullAccess.
            If None, CloudPredictor will create one with name "ag_cloud_predictor_role".
        local_output_path: str
            Path to directory where downloaded trained predictor, batch transform results, and intermediate outputs should be saved
            If unspecified, a time-stamped folder called "AutogluonCloudPredictor/ag-[TIMESTAMP]" will be created in the working directory to store all downloaded trained predictor, batch transform results, and intermediate outputs.
            Note: To call `fit()` twice and save all results of each fit, you must specify different `local_output_path` locations or don't specify `local_output_path` at all.
            Otherwise files from first `fit()` will be overwritten by second `fit()`.
        cloud_output_path: str
            Path to s3 location where intermediate artifacts will be uploaded and trained models should be saved.
            If unspecified, a time-stamped folder called "s3://ag-cloud-predictor/[TIMESTAMP]" will be created in the s3 bucket to store all models and intermediate artifacts.
            Note: To call `fit()` twice and save all results of each fit, you must specify different `cloud_output_path` locations or don't specify `cloud_output_path` at all.
            Otherwise files from first `fit()` will be overwritten by second `fit()`.
        verbosity : int, default = 2
            Verbosity levels range from 0 to 4 and control how much information is printed.
            Higher levels correspond to more detailed print statements (you can set verbosity = 0 to suppress warnings).
            If using logging, you can alternatively control amount of information printed via `logger.setLevel(L)`,
            where `L` ranges from 0 to 50 (Note: higher values of `L` correspond to fewer print statements, opposite of verbosity levels).
        """
        self.verbosity = verbosity
        set_logger_verbosity(self.verbosity)
        self.role_arn = role_arn
        if not self.role_arn:
            self.role_arn = create_sagemaker_role_and_attach_policies(
                role_name='ag_cloud_predictor_role',
                trust_relationship=SAGEMAKER_TRUST_REPLATIONSHIP,
                policies=SAGEMAKER_POLICIES
            )
        self.sagemaker_session = sagemaker.session.Session()
        self.local_output_path = self._setup_local_output_path(local_output_path)
        self.cloud_output_path = self._setup_cloud_output_path(cloud_output_path)
        self.endpoint = None

        self._region = self.sagemaker_session.boto_region_name
        self._fit_job = SageMakerFitJob(session=self.sagemaker_session)
        self._batch_transform_jobs = MostRecentInsertedOrderedDict()

        self._setup_predictor_type()

    @property
    def is_fit(self):
        return self._fit_job.completed

    @property
    def endpoint_name(self):
        """
        Return the CloudPredictor deployed endpoint name
        """
        if self.endpoint:
            return self.endpoint.endpoint_name
        return None

    def info(self):
        """
        Return general info about CloudPredictor
        """
        info = dict(
            local_output_path=self.local_output_path,
            cloud_output_path=self.cloud_output_path,
            fit_job=self._fit_job.info(),
            recent_transform_job=self._batch_transform_jobs.last_value.info() if len(self._batch_transform_jobs) > 0 else None,
            transform_jobs=[job_name for job_name in self._batch_transform_jobs.keys()],
            endpoint=self.endpoint_name
        )
        return info

    def _setup_predictor_type(self):
        self.predictor_type = None
        self._train_script_path = None
        self._serve_script_path = None

    def _setup_local_output_path(self, path):
        if path is None:
            utcnow = datetime.utcnow()
            timestamp = utcnow.strftime("%Y%m%d_%H%M%S")
            path = f'AutogluonCloudPredictor/ag-{timestamp}{os.path.sep}'
        path = setup_outputdir(path)
        util_path = os.path.join(path, 'utils')
        try:
            os.makedirs(util_path)
        except FileExistsError:
            logger.warning(f'Warning: path already exists! This predictor may overwrite an existing predictor! path="{path}"')
        return os.path.abspath(path)

    def _setup_cloud_output_path(self, path):
        if path is None:
            return f's3://ag-cloud-predictor/{sagemaker.utils.sagemaker_timestamp()}'
        if path.endswith('/'):
            path = path[:-1]
        if is_s3_url(path):
            return path
        return 's3://' + path

    def _retrieve_latest_framework_version(self, framework_type='training'):
        return retrieve_latest_framework_version(framework_type)

    def _parse_framework_version(self, framework_version, framework_type):
        if framework_version == 'latest':
            framework_version = self._retrieve_latest_framework_version(framework_type)
        else:
            valid_options = retrieve_available_framework_versions(framework_type)
            assert framework_version in valid_options, f'{framework_version} is not a valid option. Options are: {valid_options}'
        return framework_version

    def _construct_config(self, predictor_init_args, predictor_fit_args, leaderboard):
        assert self.predictor_type is not None
        config = dict(
            predictor_type=self.predictor_type,
            predictor_init_args=predictor_init_args,
            predictor_fit_args=predictor_fit_args,
            leaderboard=leaderboard,
        )
        path = os.path.join(self.local_output_path, 'utils', 'config.yaml')
        with open(path, 'w') as f:
            yaml.dump(config, f)
        return path

    def _setup_bucket(self, bucket):
        s3 = boto3.resource('s3')
        if not s3.Bucket(bucket) in s3.buckets.all():
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={
                    'LocationConstraint': self.sagemaker_session.boto_region_name,
                }
            )

    # FIXME: Remember to change output_type back to parquet when parquet is fixed in the gpu container
    def _prepare_data(self, data, filename, output_type='csv'):
        assert output_type in ['parquet', 'csv'], f'output type:{output_type} is not supported'
        path = os.path.join(self.local_output_path, 'utils')
        converter = FormatConverterFactory.get_converter(output_type)
        return converter.convert(data, path, filename)

    def _upload_fit_artifact(
        self,
        train_data,
        tune_data,
        config
    ):
        cloud_bucket, cloud_key_prefix = s3_path_to_bucket_prefix(self.cloud_output_path)
        util_key_prefix = cloud_key_prefix + '/utils'
        train_input = train_data
        if isinstance(train_data, pd.DataFrame) or not is_s3_url(train_data):
            train_data = self._prepare_data(train_data, 'train')
            logger.log(20, 'Uploading train data...')
            train_input = self.sagemaker_session.upload_data(
                path=train_data,
                bucket=cloud_bucket,
                key_prefix=util_key_prefix
            )
            logger.log(20, 'Train data uploaded successfully')

        tune_input = tune_data
        if tune_data is not None:
            if isinstance(tune_data, pd.DataFrame) or not is_s3_url(tune_data):
                tune_data = self._prepare_data(tune_data, 'tune')
                logger.log(20, 'Uploading tune data...')
                tune_input = self.sagemaker_session.upload_data(
                    path=tune_data,
                    bucket=cloud_bucket,
                    key_prefix=util_key_prefix
                )
                logger.log(20, 'Tune data uploaded successfully')

        config_input = self.sagemaker_session.upload_data(
            path=config,
            bucket=cloud_bucket,
            key_prefix=util_key_prefix
        )
        return train_input, tune_input, config_input

    def fit(
        self,
        predictor_init_args,
        predictor_fit_args,
        leaderboard=True,
        framework_version='latest',
        job_name=None,
        instance_type='ml.m5.2xlarge',
        instance_count=1,
        volume_size=30,
        wait=True,
        autogluon_sagemaker_estimator_kwargs=dict(),
        **kwargs
    ):
        """
        Fit the predictor with SageMaker.
        This function will first upload necessary config and train data to s3 bucket.
        Then launch a SageMaker training job with the AutoGluon training container.

        Parameters
        ----------
        predictor_init_args: dict
            Init args for the predictor
        predictor_fit_args: dict
            Fit args for the predictor
        leaderboard: bool, default = True
            Whether to include the leaderboard in the output artifact
        framework_version: str, default = `latest`
            Training container version of autogluon.
            If `latest`, will use the latest available container version.
            If provided a specific version, will use this version.
        job_name: str, default = None
            Name of the launched training job.
            If None, CloudPredictor will create one with prefix ag-CloudPredictor
        instance_type: str, default = 'ml.m5.2xlarge'
            Instance type the predictor will be trained on with SageMaker.
        instance_count: int, default = 1
            Number of instance used to fit the predictor.
        volumes_size: int, default = 30
            Size in GB of the EBS volume to use for storing input data during training (default: 30).
            Must be large enough to store training data if File Mode is used (which is the default).
        wait: bool, default = True
            Whether the call should wait until the job completes
            To be noticed, the function won't return immediately because there are some preparations needed prior fit.
            Use `get_fit_job_status` to get job status.
        autogluon_sagemaker_estimator_kwargs: dict, default = dict()
            Any extra arguments needed to initialize AutoGluonSagemakerEstimator
            Please refer to https://sagemaker.readthedocs.io/en/stable/api/training/estimators.html#sagemaker.estimator.Framework for all options
        **kwargs:
            Any extra arguments needed to pass to fit.
            Please refer to https://sagemaker.readthedocs.io/en/stable/api/training/estimators.html#sagemaker.estimator.Framework.fit for all options

        Returns
        -------
        `CloudPredictor` object. Returns self.
        """
        assert not self._fit_job.completed, 'Predictor is already fit! To fit additional models, create a new `CloudPredictor`'
        # TODO: Add warning for multi-model image not working properly
        train_data = predictor_fit_args.pop('train_data')
        tune_data = predictor_fit_args.pop('tuning_data', None)
        framework_version = self._parse_framework_version(framework_version, 'training')

        if not job_name:
            job_name = sagemaker.utils.unique_name_from_base("ag-CloudPredictor")

        autogluon_sagemaker_estimator_kwargs.pop('output_path', None)
        output_path = self.cloud_output_path + '/output'
        cloud_bucket, _ = s3_path_to_bucket_prefix(self.cloud_output_path)

        entry_point = TRAIN_SCRIPT_PATH
        user_entry_point = autogluon_sagemaker_estimator_kwargs.pop(entry_point, None)
        if user_entry_point:
            logger.warning(f'Providing a custom entry point could break the fit. Please refer to `{entry_point}` for our implementation')
            entry_point = user_entry_point
        else:
            # Avoid user passing in source_dir without specifying entry point
            autogluon_sagemaker_estimator_kwargs.pop('source_dir', None)

        self._setup_bucket(cloud_bucket)
        config = self._construct_config(predictor_init_args, predictor_fit_args, leaderboard)
        train_input, tune_input, config_input = self._upload_fit_artifact(
            train_data,
            tune_data,
            config
        )
        inputs = dict(
            config=config_input,
            train=train_input,
        )
        if tune_input:
            inputs['tune'] = tune_input

        self._fit_job.run(
            role=self.role_arn,
            entry_point=entry_point,
            region=self._region,
            instance_type=instance_type,
            instance_count=instance_count,
            volume_size=volume_size,
            framework_version=framework_version,
            base_job_name="autogluon-cloudpredictor-train",
            output_path=output_path,
            inputs=inputs,
            wait=wait,
            job_name=job_name,
            autogluon_sagemaker_estimator_kwargs=autogluon_sagemaker_estimator_kwargs,
            **kwargs
        )
        return self

    def attach_job(self, job_name):
        """
        Attach to a sagemaker training job.
        This is useful when the local process crashed and you want to reattach to the previous job

        Parameters
        ----------
        job_name: str
            The name of the job being attached

        Returns
        -------
        `CloudPredictor` object. Returns self.
        """
        self._fit_job = SageMakerFitJob.attach(job_name)
        return self

    def get_fit_job_status(self):
        """
        Get the status of the training job.
        This is useful when the user made an asynchronous call to the `fit()` function

        Returns
        -------
        str,
        Valid Values: InProgress | Completed | Failed | Stopping | Stopped | NotCreated
        """
        return self._fit_job.get_job_status()

    def download_trained_predictor(self, save_path=None):
        """
        Download the trained predictor from the cloud.

        Parameters
        ----------
        save_path: str
            Path to save the model.
            If None, CloudPredictor will create a folder 'AutogluonModels' for the model under `local_output_path`.

        Returns
        -------
        save_path: str
            Path to the saved model.
        """
        path = self._fit_job.get_output_path()
        if not save_path:
            save_path = self.local_output_path
        save_path = self._download_predictor(path, save_path)
        return save_path

    def _get_local_predictor_cls(self):
        raise NotImplementedError

    def to_local_predictor(self, save_path=None):
        """
        Convert the SageMaker trained predictor to a local TabularPredictor or TextPredictor.

        Parameters
        ----------
        save_path: str
            Path to save the model.
            If None, CloudPredictor will create a folder for the model.

        Returns
        -------
        AutoGluon Predictor,
            TabularPredictor or TextPredictor based on `predictor_type`
        """
        predictor_cls = self._get_local_predictor_cls()
        local_model_path = self.download_trained_predictor(save_path)
        return predictor_cls.load(local_model_path)

    def _upload_predictor(self, predictor_path, key_prefix):
        cloud_bucket = s3_path_to_bucket_prefix(self.cloud_output_path)
        if not is_s3_url(predictor_path):
            if os.path.isfile(predictor_path):
                if tarfile.is_tarfile(predictor_path):
                    predictor_path = self.sagemaker_session.upload_data(
                        path=predictor_path,
                        bucket=cloud_bucket,
                        key_prefix=key_prefix
                    )
                else:
                    raise ValueError('Please provide a tarball containing the model')
            else:
                raise ValueError('Please provide a valid path to the model tarball.')
        return predictor_path

    def deploy(
        self,
        predictor_path=None,
        endpoint_name=None,
        framework_version='latest',
        instance_type='ml.m5.2xlarge',
        initial_instance_count=1,
        wait=True,
        autogluon_sagemaker_inference_model_kwargs=dict(),
        **kwargs
    ):
        """
        Deploy a predictor as a SageMaker endpoint, which can be used to do real-time inference later.
        This method would first create a AutoGluonSagemakerInferenceModel with the trained predictor,
        and then deploy it to the endpoint.

        Parameters
        ----------
        predictor_path: str
            Path to the predictor tarball you want to deploy.
            Path can be both a local path or a S3 location.
            If None, will deploy the most recent trained predictor trained with `fit()`.
        endpoint_name: str
            The endpoint name to use for the deployment.
            If None, CloudPredictor will create one
        framework_version: str, default = `latest`
            Inference container version of autogluon.
            If `latest`, will use the latest available container version.
            If provided a specific version, will use this version.
        instance_type: str, default = 'ml.m5.2xlarge'
            Instance to be deployed for the endpoint
        initial_instance_count: int, default = 1,
            Initial number of instances to be deployed for the endpoint
        wait: Bool, default = True,
            Whether to wait for the endpoint to be deployed.
            To be noticed, the function won't return immediately because there are some preparations needed prior deployment.
        autogluon_sagemaker_inference_model_kwargs: dict, default = dict()
            Any extra arguments needed to initialize AutoGluonSagemakerInferenceModel
            Please refer to https://sagemaker.readthedocs.io/en/stable/api/inference/model.html#sagemaker.model.FrameworkModel for all options
        **kwargs:
            Any extra arguments needed to pass to deploy.
            Please refer to https://sagemaker.readthedocs.io/en/stable/api/inference/model.html#sagemaker.model.Model.deploy for all options
        """
        assert self.endpoint is None, 'There is an endpoint already attached. Either detach it with `detach` or clean it up with `cleanup_deployment`'
        if not predictor_path:
            predictor_path = self._fit_job.get_output_path()
            assert predictor_path, 'No cloud trained model found.'
        predictor_path = self._upload_predictor(predictor_path, f'endpoints/{endpoint_name}/predictor')

        if not endpoint_name:
            endpoint_name = sagemaker.utils.unique_name_from_base("sagemaker-autogluon-serving-trained-model")
        framework_version = self._parse_framework_version(framework_version, 'inference')

        assert self._serve_script_path is not None
        entry_point = self._serve_script_path
        user_entry_point = autogluon_sagemaker_inference_model_kwargs.pop('entry_point', None)
        if user_entry_point:
            logger.warning(f'Providing a custom entry point could break the deployment. Please refer to `{entry_point}` for our implementation')
            entry_point = user_entry_point

        predictor_cls = AutoGluonRealtimePredictor
        user_predictor_cls = autogluon_sagemaker_inference_model_kwargs.pop('predictor_cls', None)
        if user_predictor_cls:
            logger.warning('Providing a custom predictor_cls could break the deployment. Please refer to `AutoGluonRealtimePredictor` for how to provide a custom predictor')
            predictor_cls = user_predictor_cls

        model = AutoGluonSagemakerInferenceModel(
            model_data=predictor_path,
            role=self.role_arn,
            region=self._region,
            framework_version=framework_version,
            instance_type=instance_type,
            entry_point=entry_point,
            predictor_cls=predictor_cls,
            **autogluon_sagemaker_inference_model_kwargs
        )

        logger.log(20, 'Deploying model to the endpoint')
        self.endpoint = model.deploy(
            endpoint_name=endpoint_name,
            instance_type=instance_type,
            initial_instance_count=initial_instance_count,
            wait=wait,
            **kwargs
        )

    def attach_endpoint(self, endpoint):
        """
        Attach the current CloudPredictor to an existing SageMaker endpoint.

        Parameters
        ----------
        endpoint: str or  :class:`AutoGluonRealtimePredictor`
            If str is passed, it should be the name of the endpoint being attached to.
        """
        assert self.endpoint is None, 'There is an endpoint already attached. Either detach it with `detach` or clean it up with `cleanup_deployment`'
        if type(endpoint) == str:
            self.endpoint = AutoGluonRealtimePredictor(
                endpoint_name=endpoint,
                sagemaker_session=self.sagemaker_session,
            )
        elif isinstance(endpoint, AutoGluonRealtimePredictor):
            self.endpoint = endpoint
        else:
            raise ValueError('Please provide either an endpoint name or an endpoint of type `AutoGluonRealtimePredictor`')

    def detach_endpoint(self):
        """
        Detach the current endpoint and return it.

        Returns
        -------
        `AutoGluonRealtimePredictor` object.
        """
        assert self.endpoint is not None
        detached_endpoint = self.endpoint
        self.endpoint = None
        return detached_endpoint

    def predict_real_time(self, test_data, accept='application/x-parquet'):
        """
        Predict with the deployed SageMaker endpoint. A deployed SageMaker endpoint is required.
        This is intended to provide a low latency inference.
        If you want to inference on a large dataset, use `predict()` instead.

        Parameters
        ----------
        test_data: Union(str, pandas.DataFrame)
            The test data to be inferenced. Can be a pandas.DataFrame, a local path or a s3 path.
        accept: str, default = application/x-parquet
            Type of accept output content.
            Valid options are application/x-parquet, text/csv, application/json

        Returns
        -------
        Pandas.DataFrame
        Predict results in DataFrame
        """
        assert self.endpoint, 'Please call `deploy()` to deploy an endpoint first.'
        assert accept in VALID_ACCEPT, f'Invalid accept type. Options are {VALID_ACCEPT}.'
        if type(test_data) == str:
            test_data = load_pd.load(test_data)
        memory_usage = test_data.memory_usage(index=False, deep=True).sum()
        if memory_usage > 5e6:
            logger.warning(f'Large test data detected({memory_usage // 10e6} MB). SageMaker endpoint could only take maximum 5MB. The prediction is likely to fail')
            logger.warning('Please consider reduce test data size or use `predict()` instead.')
        if not isinstance(test_data, pd.DataFrame):
            raise ValueError('test_data must be either a pandas.DataFrame, a local path or a s3 path')
        return self.endpoint.predict(test_data, initial_args={'Accept': accept})

    def predict(
        self,
        test_data,
        predictor_path=None,
        framework_version='latest',
        job_name=None,
        instance_type='ml.m5.2xlarge',
        instance_count=1,
        wait=True,
        autogluon_sagemaker_inference_model_kwargs=dict(),
        transformer_kwargs=dict(),
        **kwargs,
    ):
        """
        Predict using SageMaker batch transform.
        When minimizing latency isn't a concern, then the batch transform functionality may be easier, more scalable, and more appropriate.
        If you want to minimize latency, use `predict_real_time()` instead.
        This method would first create a AutoGluonSagemakerInferenceModel with the trained predictor,
        then create a transformer with it, and call transform in the end.

        Parameters
        ----------
        test_data: Union(str, pandas.DataFrame)
            The test data to be inferenced. Can be a pandas.DataFrame, a local path or a s3 path.
        predictor_path: str
            Path to the predictor tarball you want to use to predict.
            Path can be both a local path or a S3 location.
            If None, will use the most recent trained predictor trained with `fit()`.
        framework_version: str, default = `latest`
            Inference container version of autogluon.
            If `latest`, will use the latest available container version.
            If provided a specific version, will use this version.
        job_name: str, default = None
            Name of the launched training job.
            If None, CloudPredictor will create one with prefix ag-CloudPredictor-batch-transform.
        instance_count: int, default = 1,
            Number of instances used to do batch transform.
        instance_type: str, default = 'ml.m5.2xlarge'
            Instance to be used for batch transform.
        wait: bool, default = True
            Whether to wait for batch transform to complete.
            To be noticed, the function won't return immediately because there are some preparations needed prior transform.
        autogluon_sagemaker_inference_model_kwargs: dict, default = dict()
            Any extra arguments needed to initialize AutoGluonSagemakerInferenceModel
            Please refer to https://sagemaker.readthedocs.io/en/stable/api/inference/model.html#sagemaker.model.FrameworkModel for all options
        transformer_kwargs: dict
            Any extra arguments needed to pass to transformer.
            Please refer to https://sagemaker.readthedocs.io/en/stable/api/inference/transformer.html#sagemaker.transformer.Transformer for all options.
        **kwargs:
            Any extra arugments needed to pass to transform.
            Please refer to https://sagemaker.readthedocs.io/en/stable/api/inference/transformer.html#sagemaker.transformer.Transformer.transform for all options.
        """
        # Sagemaker batch transformation is able to take in headers during the most recent test
        # logger.warning('Please remove headers of the test data and make sure the columns are in the same order as the training data.')
        if not predictor_path:
            predictor_path = self._fit_job.get_output_path()
            assert predictor_path, 'No cloud trained model found.'

        framework_version = self._parse_framework_version(framework_version, 'inference')

        output_path = kwargs.get('output_path', None)
        if not output_path:
            output_path = self.cloud_output_path
        assert is_s3_url(output_path)
        output_path = output_path + '/batch_transform' + f'/{sagemaker.utils.sagemaker_timestamp()}'

        cloud_bucket, cloud_key_prefix = s3_path_to_bucket_prefix(output_path)
        logger.log(20, 'Preparing autogluon predictor...')
        predictor_path = self._upload_predictor(predictor_path, cloud_key_prefix + '/predictor')

        if not job_name:
            job_name = sagemaker.utils.unique_name_from_base("ag-CloudPredictor-batch-transform")

        if isinstance(test_data, pd.DataFrame) or not is_s3_url(test_data):
            test_data = self._prepare_data(test_data, 'test', output_type='csv')
            logger.log(20, 'Uploading data...')
            test_input = self.sagemaker_session.upload_data(
                path=test_data,
                bucket=cloud_bucket,
                key_prefix=cloud_key_prefix + '/data'
            )
            logger.log(20, 'Data uploaded successfully')
        else:
            test_input = test_data

        assert self._serve_script_path is not None
        entry_point = self._serve_script_path
        user_entry_point = autogluon_sagemaker_inference_model_kwargs.pop('entry_point', None)
        if user_entry_point:
            entry_point = user_entry_point

        predictor_cls = AutoGluonBatchPredictor
        user_predictor_cls = autogluon_sagemaker_inference_model_kwargs.pop('predictor_cls', None)
        if user_predictor_cls:
            logger.warning('Providing a custom predictor_cls could break the deployment. Please refer to `AutoGluonBatchPredictor` for how to provide a custom predictor')
            predictor_cls = user_predictor_cls

        split_type = kwargs.pop('split_type', None)
        content_type = kwargs.pop('content_type', None)
        if not split_type:
            split_type = 'Line'
        if not content_type:
            content_type = 'text/csv'

        batch_transform_job = SageMakerBatchTransformationJob(session=self.sagemaker_session)
        batch_transform_job.run(
            model_data=predictor_path,
            role=self.role_arn,
            region=self._region,
            framework_version=framework_version,
            instance_count=instance_count,
            instance_type=instance_type,
            entry_point=entry_point,
            predictor_cls=predictor_cls,
            output_path=output_path + '/results',
            test_input=test_input,
            job_name=job_name,
            split_type=split_type,
            content_type=content_type,
            wait=wait,
            transformer_kwargs=transformer_kwargs,
            autogluon_sagemaker_inference_model_kwargs=autogluon_sagemaker_inference_model_kwargs,
            **kwargs
        )
        self._batch_transform_jobs[job_name] = batch_transform_job

    def download_predict_results(self, job_name=None, save_path=None):
        """
        Download batch transform result

        Parameters
        ----------
        job_name: str
            The specific batch transform job result to download.
            If None, will download the most recent job result.
        save_path: str
            Path to save the downloaded result.
            If None, CloudPredictor will create one.
        """
        if not job_name:
            job_name = self._batch_transform_jobs.last
        assert job_name is not None, 'There is no batch transform job.'
        job = self._batch_transform_jobs.get(job_name, None)
        assert job is not None, f'Could not find the batch transform job that matches name {job_name}'
        result_path = job.get_output_path()
        assert result_path is not None, 'No predict results found.'
        file_name = result_path.split('/')[-1]
        if not save_path:
            save_path = self.local_output_path
        save_path = os.path.expanduser(save_path)
        save_path = os.path.abspath(save_path)
        results_save_path = os.path.join(save_path, 'batch_transform', job_name)
        if not os.path.isdir(results_save_path):
            os.makedirs(results_save_path)
        temp_results_save_path = os.path.join(results_save_path, file_name)
        if os.path.isfile(temp_results_save_path):
            logger.warning('File already exists. Will rename the file to avoid overwrite.')
            file_name = rename_file_with_uuid(file_name)
        results_save_path = os.path.join(results_save_path, file_name)
        results_bucket, results_key_prefix = s3_path_to_bucket_prefix(result_path)
        download_s3_file(results_bucket, results_key_prefix, results_save_path)
        logger.info(20, f'Results have been saved to {results_save_path}')

    def get_batch_transform_job_status(self, job_name=None):
        """
        Get the status of the batch transform job.
        This is useful when the user made an asynchronous call to the `predict()` function

        Parameters
        ----------
        job_name: str
            The name of the job being checked.
            If None, will check the most recent job status.

        Returns
        -------
        str,
        Valid Values: InProgress | Completed | Failed | Stopping | Stopped | NotCreated
        """
        if not job_name:
            job_name = self._batch_transform_jobs.last
        job = self._batch_transform_jobs.get(job_name, None)
        if job:
            return job.get_job_status()
        return 'NotCreated'

    def cleanup_deployment(self):
        """
        Delete endpoint, endpoint configuration and deployed model
        """
        self._delete_endpoint_model()
        self._delete_endpoint()

    def _delete_endpoint(self, delete_endpoint_config=True):
        assert self.endpoint, 'There is no endpoint deployed yet'
        logger.log(20, 'Deleteing endpoint')
        self.endpoint.delete_endpoint(delete_endpoint_config=delete_endpoint_config)
        logger.log(20, 'Endpoint deleted')
        self.endpoint = None

    def _delete_endpoint_model(self):
        assert self.endpoint, 'There is no endpoint deployed yet'
        logger.log(20, 'Deleting endpoint model')
        self.endpoint.delete_model()
        logger.log(20, 'Endpoint model deleted')

    def _download_predictor(self, path, save_path):
        logger.log(20, 'Downloading trained models to local directory')
        predictor_bucket, predictor_key_prefix = s3_path_to_bucket_prefix(path)
        tarball_path = os.path.join(save_path, 'model.tar.gz')
        download_s3_file(predictor_bucket, predictor_key_prefix, tarball_path)
        logger.log(20, 'Extracting the trained model tarball')
        save_path = os.path.join(save_path, 'AutogluonModels')
        unzip_file(tarball_path, save_path)
        return save_path

    def save(self, silent=False):
        """
        Save the CloudPredictor so that user can later reload the predictor to gain access to deployed endpoint.
        """
        path = self.local_output_path
        predictor_file_name = self.predictor_file_name
        temp_session = self.sagemaker_session
        temp_region = self._region
        self.sagemaker_session = None
        self._region = None
        temp_endpoint = None
        if self.endpoint:
            temp_endpoint = self.endpoint
            self._endpoint_saved = self.endpoint_name
            self.endpoint = None

        save_pkl.save(path=os.path.join(path, predictor_file_name), object=self)
        self.sagemaker_session = temp_session
        self._region = temp_region
        if temp_endpoint:
            self.endpoint = temp_endpoint
            self._endpoint_saved = None
        if not silent:
            logger.log(20, f'{type(self).__name__} saved. To load, use: predictor = {type(self).__name__}.load("{self.local_output_path}")')

    def _load_jobs(self):
        self._fit_job.session = self.sagemaker_session
        for job in self._batch_transform_jobs:
            job.session = self.sagemaker_session

    @classmethod
    def load(cls, path, verbosity=None):
        """
        Load the CloudPredictor

        Parameters
        ----------
        path: str
            The path to directory in which this Predictor was previously saved

        Returns
        -------
        `CloudPredictor` object.
        """
        if verbosity is not None:
            set_logger_verbosity(verbosity, logger=logger)  # Reset logging after load (may be in new Python session)
        if path is None:
            raise ValueError("path cannot be None in load()")

        path = setup_outputdir(path, warn_if_exist=False)  # replace ~ with absolute path if it exists
        predictor: CloudPredictor = load_pkl.load(path=os.path.join(path, cls.predictor_file_name))
        predictor.sagemaker_session = sagemaker.session.Session()
        predictor._region = predictor.sagemaker_session.boto_region_name
        predictor._load_jobs()
        if hasattr(predictor, '_endpoint_saved') and predictor._endpoint_saved:
            predictor.attach_endpoint(predictor._endpoint_saved)
            predictor._endpoint_saved = None
        # TODO: Version compatibility check
        return predictor


class TabularCloudPredictor(CloudPredictor):

    predictor_file_name = 'TabularCloudPredictor.pkl'

    def _setup_predictor_type(self):
        self.predictor_type = 'tabular'
        self._train_script_path = TRAIN_SCRIPT_PATH
        self._serve_script_path = TABULAR_SERVE_SCRIPT_PATH

    def _get_local_predictor_cls(self):
        from autogluon.tabular import TabularPredictor
        predictor_cls = TabularPredictor
        return predictor_cls


class TextCloudPredictor(CloudPredictor):

    predictor_file_name = 'TextCloudPredictor.pkl'

    def _setup_predictor_type(self):
        self.predictor_type = 'text'
        self._train_script_path = TRAIN_SCRIPT_PATH
        self._serve_script_path = TEXT_SERVE_SCRIPT_PATH

    def _get_local_predictor_cls(self):
        from autogluon.text import TextPredictor
        predictor_cls = TextPredictor
        return predictor_cls
