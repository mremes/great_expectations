import os
import json
import logging

from great_expectations.version import __version__
from great_expectations.dataset import PandasDataset
from great_expectations import read_csv
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class DataContext(object):
    """A generic DataContext, exposing the base API including constructor with `options` parameter, list_datasets,
    and get_dataset.

    Warning: this feature is new in v0.4 and may change based on community feedback.
    """

    def __init__(self, options=None, *args, **kwargs):
        self.connect(options, *args, **kwargs)

    def connect(self, options):
        # TODO: Revisit this logic to better at making real guesses
        if options is None:
            if os.path.isdir("../notebooks") and os.path.isdir("../../great_expectations"):
                self.directory = "../data_asset_configurations"
            else:
                self.directory = "./great_expectations/data_asset_configurations"
        else:
            if os.path.isdir(os.path.join(options, "great_expectations")):
                self.directory = options + "/great_expectations/data_asset_configurations"
            else:
                self.directory = os.path.join(options, "great_expectations/data_asset_configurations")
        self.validation_params = {}
        self._project_config = {} # TODO: read from .yml file if available
        self._compiled = False

    def list_data_asset_configs(self, show_full_path=False):
        if show_full_path:
            return [os.path.abspath(os.path.join(self.directory, file_path)) for file_path in os.listdir(self.directory) if file_path.endswith('.json')]
        else:
            return [os.path.splitext(os.path.basename(file_path))[0] for file_path in os.listdir(self.directory) if file_path.endswith('.json')]

    def get_data_asset_config(self, data_asset_name):
        config_file_path = os.path.join(self.directory, data_asset_name + '.json')
        if os.path.isfile(config_file_path):
            with open(os.path.join(self.directory, data_asset_name + '.json')) as json_file:
                return json.load(json_file)
        else:
            #TODO (Eugene): Would it be better to return None if the file does not exist? Currently this method acts as
            # get_or_create
            return {
                'data_asset_name': data_asset_name,
                'meta': {
                    'great_expectations.__version__': __version__
                },
                'expectations': [],
             }

    def save_data_asset_config(self, data_asset_config):
        data_asset_name = data_asset_config['data_asset_name']
        config_file_path = os.path.join(self.directory, data_asset_name + '.json')
        with open(config_file_path, 'w') as outfile:
            json.dump(data_asset_config, outfile)
        self._compiled = False

    def bind_evaluation_parameters(self, run_id, expectations_config):
        return self.validation_params[run_id] if run_id in self.validation_params else {}

    def register_validation_results(self, run_id, validation_results):
        if not self._compiled:
            self._compile()

        if not "data_asset_name" in validation_results["meta"]:
            logger.warning("No data_asset_name found in validation results; evaluation parameters cannot be registered.")
            return
        elif validation_results["meta"]["data_asset_name"] not in self._compiled_parameters["data_assets"]:
            # This is fine; short-circuit since we do not need to register any results from this dataset.
            return
        else:
            data_asset_name = validation_results["meta"]["data_asset_name"]
        
        for result in validation_results['results']:
            # Unoptimized: loop over all results and check if each is needed
            if result['expectation_config']['expectation_type'] in self._compiled_parameters["data_assets"][data_asset_name]:
                if "column" in result['expectation_config']['kwargs'] and \
                    result['expectation_config']['kwargs']["column"] in self._compiled_parameters["data_assets"][data_asset_name]["columns"]:
                    column = result['expectation_config']['kwargs']["column"]
                    # Now that we have a small search space, invert logic, and look for the parameters in our result
                    for type_key, desired_parameters in self._compiled_parameters["data_assets"][data_asset_name]["columns"][column].items():
                        # value here is the set of desired parameters under the type_key
                        for desired_param in desired_parameters:
                            desired_key = desired_param.split(":")[-1]
                            if type_key == "result" and desired_key in result['result']:
                                self.store_validation_param(desired_param, result["result"][desired_key])
                            elif type_key == "details" and desired_key in result["result"]["details"]:
                                self.store_validation_param(desired_param, result["result"]["details"])
                            else:
                                logger.warning("Unrecognized key for parameter %s" % desired_param)
                
                for type_key, desired_parameters in self._compiled_parameters["data_assets"][data_asset_name]:
                    if type_key == "columns":
                        continue
                    for desired_param in desired_parameters:
                        desired_key = desired_param.split(":")[-1]
                        if type_key == "result" and desired_key in result['result']:
                            self.store_validation_param(desired_param, result["result"][desired_key])
                        elif type_key == "details" and desired_key in result["result"]["details"]:
                            self.store_validation_param(desired_param, result["result"]["details"])
                        else:
                            logger.warning("Unrecognized key for parameter %s" % desired_param)

    def store_validation_param(self, key, value):
        self.validation_params.update({key: value})

    def get_validation_param(self, key):
        try:
            return self.validation_params["key"]
        except KeyError:
            return None

    def _compile(self):
        """Compiles all current expectation configurations in this context to be ready for reseult registration.
        
        Compilation only respects parameters with a URN structure beginning with urn:great_expectations:validations
        It splits parameters by the : (colon) character; valid URNs must have one of the following structures to be
        automatically recognized.

        "urn" : "great_expectations" : "validations" : data_asset_name : "expectations" : expectation_name : "columns" : column_name : "result": result_key
         [0]            [1]                 [2]              [3]              [4]              [5]              [6]          [7]         [8]        [9]
        
        "urn" : "great_expectations" : "validations" : data_asset_name : "expectations" : expectation_name : "columns" : column_name : "details": details_key
         [0]            [1]                 [2]              [3]              [4]              [5]              [6]          [7]         [8]        [9]

        "urn" : "great_expectations" : "validations" : data_asset_name : "expectations" : expectation_name : "result": result_key
         [0]            [1]                 [2]              [3]              [4]              [5]            [6]          [7]  

        "urn" : "great_expectations" : "validations" : data_asset_name : "expectations" : expectation_name : "details": details_key
         [0]            [1]                 [2]              [3]              [4]              [5]             [6]          [7]  

         Parameters are compiled to the following structure:
         {
             "raw": <set of all parameters requested>
             "data_assets": {
                 data_asset_name: {
                    expectation_name: {
                        "details": <set of details parameter values requested>
                        "result": <set of result parameter values requested>
                        column_name: {
                            "details": <set of details parameter values requested>
                            "result": <set of result parameter values requested>
                        }
                    }
                 }
             }
         }

        """

        # Full recompilation every time
        self._compiled_parameters = {
            "raw": set(),
            "data_assets": {}
        }

        known_assets = self.list_data_asset_configs()

        for config_file in self.list_data_asset_configs(show_full_path=True):
            config = json.load(open(config_file, 'r'))
            for expectation in config["expectations"]:
                for _, value in expectation["kwargs"].items():
                    if isinstance(value, dict) and '$PARAMETER' in value:
                        # Compile only respects parameters in urn structure beginning with urn:great_expectations:validations
                        if value["$PARAMETER"].startswith("urn:great_expectations:validations:"):
                            parameter = value["$PARAMETER"]
                            self._compiled_parameters["raw"].add(parameter)
                            param_parts = parameter.split(":")
                            try:
                                if param_parts[3] not in known_assets:
                                    logger.warning("Adding parameter %s for unknown data asset config" % parameter)

                                if param_parts[3] not in self._compiled_parameters["data_assets"]:
                                    self._compiled_parameters["data_assets"][param_parts[3]] = {}

                                if param_parts[5] not in self._compiled_parameters["data_assets"][param_parts[3]]:
                                    self._compiled_parameters["data_assets"][param_parts[3]][param_parts[5]] = {}

                                if param_parts[6] in ["results", "details"]:
                                    if param_parts[6] not in self._compiled_parameters["data_assets"][param_parts[3]][param_parts[5]]:
                                        self._compiled_parameters["data_assets"][param_parts[3]][param_parts[5]] = set()
                                    self._compiled_parameters["data_assets"][param_parts[3]][param_parts[5]][param_parts[6]].add(parameter)
                                
                                elif param_parts[6] == "columns":
                                    if param_parts[7] not in self._compiled_parameters["data_assets"][param_parts[3]][param_parts[5]]:
                                        self._compiled_parameters["data_assets"][param_parts[3]][param_parts[5]][param_parts[7]] = {}
                                    if param_parts[8] not in self._compiled_parameters["data_assets"][param_parts[3]][param_parts[5]][param_parts[7]]:
                                        self._compiled_parameters["data_assets"][param_parts[3]][param_parts[5]][param_parts[7]][param_parts[8]] = set()
                                    self._compiled_parameters["data_assets"][param_parts[3]][param_parts[5]][param_parts[7]][param_parts[8]].add(parameter)                                  
                                
                                else:
                                    logger.warning("Invalid parameter urn (unrecognized structure): %s" % parameter)
                            except IndexError:
                                logger.warning("Invalid parameter urn (not enough parts): %s" % parameter)
        self._compiled = True

    def review_validation_result(self, url, failed_only=False):
        url = url.strip()
        if url.startswith("s3://"):
            try:
                import boto3
                s3 = boto3.client('s3')
            except ImportError:
                raise ImportError("boto3 is required for retrieving a dataset from s3")
        
            parsed_url = urlparse(url)
            bucket = parsed_url.netloc
            key = parsed_url.path[1:]
            
            s3_response_object = s3.get_object(Bucket=bucket, Key=key)
            object_content = s3_response_object['Body'].read()
            
            results_dict = json.loads(object_content)

            if failed_only:
                failed_results_list = [result for result in results_dict["results"] if not result["success"]]
                results_dict["results"] = failed_results_list
                return results_dict
            else:
                return results_dict
        else:
            raise ValueError("Only s3 urls are supported.")

    def get_failed_dataset(self, validation_result, **kwargs):
        try:
            reference_url = validation_result["meta"]["dataset_reference"]
        except KeyError:
            raise ValueError("Validation result must have a dataset_reference in the meta object to fetch")
        
        if reference_url.startswith("s3://"):
            try:
                import boto3
                s3 = boto3.client('s3')
            except ImportError:
                raise ImportError("boto3 is required for retrieving a dataset from s3")
        
            parsed_url = urlparse(reference_url)
            bucket = parsed_url.netloc
            key = parsed_url.path[1:]
            
            s3_response_object = s3.get_object(Bucket=bucket, Key=key)
            if key.endswith(".csv"):
                # Materialize as dataset
                # TODO: check the associated config for the correct data_asset_type to use
                return read_csv(s3_response_object['Body'], **kwargs)
            else:
                return s3_response_object['Body']

        else:
            raise ValueError("Only s3 urls are supported.")