import json
import logging
import os
import time
from subprocess import DEVNULL, PIPE  # nosec
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set
from uuid import uuid4

import boto3
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.storage import StorageManagementClient
from azure.storage.blob import BlobServiceClient
from botocore.config import Config
from botocore.exceptions import ClientError
from google.api_core.exceptions import ClientError as GoogleClientError
from google.api_core.exceptions import Conflict
from google.cloud import storage  # type: ignore
from google.cloud.exceptions import NotFound
from googleapiclient import discovery
from googleapiclient.errors import HttpError
from oauth2client.client import GoogleCredentials

from opta.amplitude import amplitude_client
from opta.core.aws import AWS, get_aws_resource_id
from opta.core.azure import Azure
from opta.core.gcp import GCP
from opta.exceptions import MissingState, UserErrors
from opta.nice_subprocess import nice_run
from opta.utils import deep_merge, fmt_msg, logger

if TYPE_CHECKING:
    from opta.layer import Layer
EXTRA_ENV = (
    {"KUBE_CONFIG_PATH": "~/.kube/config"} if os.path.isfile("~/.kube/config") else {}
)


class Terraform:
    downloaded_state: Dict[str, Dict[Any, Any]] = {}

    @classmethod
    def init(cls, quiet: Optional[bool] = False, *tf_flags: str) -> None:
        kwargs: Dict[str, Any] = {"env": {**os.environ.copy(), **EXTRA_ENV}}
        if quiet:
            kwargs["stderr"] = PIPE
            kwargs["stdout"] = DEVNULL
        nice_run(["terraform", "init", *tf_flags], check=True, **kwargs)

    # Get outputs of the current terraform state
    @classmethod
    def get_outputs(cls, layer: "Layer") -> dict:
        state = cls.get_state(layer)
        outputs = state.get("outputs", {})
        cleaned_outputs = {}
        for k, v in outputs.items():
            cleaned_outputs[k] = v.get("value")
        return cleaned_outputs

    @classmethod
    def get_version(cls) -> str:
        out = nice_run(
            ["terraform", "version", "-json"], check=True, capture_output=True
        ).stdout.decode("utf-8")
        terraform_data = json.loads(out)
        return terraform_data["terraform_version"]

    # Get the full terraform state.
    @classmethod
    def get_state(cls, layer: "Layer") -> dict:
        if layer.name in cls.downloaded_state:
            return cls.downloaded_state[layer.name]
        if cls.download_state(layer):
            return cls.downloaded_state[layer.name]
        raise MissingState(f"Unable to download state for layer {layer.name}")

    @classmethod
    def apply(
        cls,
        layer: "Layer",
        *tf_flags: str,
        no_init: Optional[bool] = False,
        quiet: Optional[bool] = False,
    ) -> None:
        if not no_init:
            cls.init(quiet)
        kwargs: Dict[str, Any] = {"env": {**os.environ.copy(), **EXTRA_ENV}}
        if quiet:
            kwargs["stderr"] = PIPE
            kwargs["stdout"] = DEVNULL

        try:
            nice_run(
                ["terraform", "apply", "-compact-warnings", *tf_flags],
                check=True,
                **kwargs,
            )
        except Exception as e:
            logging.error(e)
            logging.info("Terraform apply failed, would rollback, but skipping for now..")
            raise e
            # cls.rollback(layer)

    @classmethod
    def rollback(cls, layer: "Layer") -> None:
        amplitude_client.send_event(amplitude_client.ROLLBACK_EVENT)

        aws_resources = AWS(layer).get_opta_resources()
        terraform_resources = set(cls.get_existing_module_resources(layer))

        # Import all stale resources into terraform state (so they can be destroyed later).
        stale_resources = []
        for resource in aws_resources:
            if resource in terraform_resources:
                continue

            try:
                resource_id = get_aws_resource_id(aws_resources[resource])
                cls.import_resource(resource, resource_id)
                stale_resources.append(resource)
            except Exception:
                logging.debug(
                    f"Resource {resource_id} failed to import. It probably no longer exists, skipping."
                )
                continue

        # Skip destroy if no resources are stale.
        if len(stale_resources) == 0:
            return None

        # Destroy stale terraform resources.
        cls.destroy_resources(layer, stale_resources)

    @classmethod
    def import_resource(cls, tf_resource_address: str, aws_resource_id: str) -> None:
        kwargs: Dict[str, Any] = {"env": {**os.environ.copy(), **EXTRA_ENV}}
        nice_run(
            ["terraform", "import", tf_resource_address, aws_resource_id],
            check=True,
            **kwargs,
        )

    @classmethod
    def refresh(cls, *tf_flags: str) -> None:
        kwargs: Dict[str, Any] = {"env": {**os.environ.copy(), **EXTRA_ENV}}
        nice_run(["terraform", "refresh", *tf_flags], check=True, **kwargs)

    @classmethod
    def destroy_resources(
        cls, layer: "Layer", target_resources: List[str], *tf_flags: str
    ) -> None:
        # If no targets are passed, "terraform destroy" attempts to destroy ALL
        # resources, which should be avoided unless explicitly done.
        if len(target_resources) == 0:
            raise Exception(
                "Target resources was specified to be destroyed, but contained an empty list"
            )

        # Refreshing the state is necessary to update terraform outputs.
        # This includes fetching the latest EKS cluster auth token, which is
        # necessary for destroying many k8s resources.
        cls.refresh()
        kwargs: Dict[str, Any] = {"env": {**os.environ.copy(), **EXTRA_ENV}}

        for module in reversed(layer.modules):
            module_address_prefix = f"module.{module.name}"
            module_resources = [
                resource
                for resource in target_resources
                if module_address_prefix in resource
            ]
            if len(module_resources) == 0:
                continue

            resource_targets = [f"-target={resource}" for resource in module_resources]
            nice_run(
                ["terraform", "destroy", *resource_targets, *tf_flags],
                check=True,
                **kwargs,
            )

    @classmethod
    def destroy_all(cls, layer: "Layer", *tf_flags: str) -> None:
        existing_modules = Terraform.get_existing_modules(layer)

        # Refreshing the state is necessary to update terraform outputs.
        # This includes fetching the latest EKS cluster auth token, which is
        # necessary for destroying many k8s resources.
        cls.refresh()

        kwargs: Dict[str, Any] = {"env": {**os.environ.copy(), **EXTRA_ENV}}
        idx = len(layer.modules) - 1
        for module in reversed(layer.modules):
            module_address_prefix = f"module.{module.name}"

            if module.name not in existing_modules:
                idx -= 1
                continue
            cls.refresh(f"-target={module_address_prefix}")
            nice_run(
                ["terraform", "destroy", f"-target={module_address_prefix}", *tf_flags],
                check=True,
                **kwargs,
            )
            layer.post_delete(idx)
            idx -= 1

        # After the layer is completely deleted, remove the opta config from the state bucket.
        if layer.cloud == "aws":
            aws = AWS(layer)
            aws.delete_opta_config()
            aws.delete_remote_state()
        elif layer.cloud == "google":
            gcp = GCP(layer)
            gcp.delete_opta_config()
            gcp.delete_remote_state()
        elif layer.cloud == "azurerm":
            azure = Azure(layer)
            azure.delete_opta_config()
            azure.delete_remote_state()
        else:
            raise Exception(
                f"Can not handle opta config deletion for cloud {layer.cloud}"
            )

        # If this is the env layer, delete the state bucket & dynamo table as well.
        if layer.name == layer.root().name:

            logger.info(f"Deleting the state storage for {layer.name}...")
            if layer.cloud == "aws":
                cls._aws_delete_state_storage(layer)
            elif layer.cloud == "google":
                cls._gcp_delete_state_storage(layer)

    # Remove a resource from the terraform state, but does not destroy it.
    @classmethod
    def remove_from_state(cls, resource_address: str) -> None:
        kwargs: Dict[str, Any] = {"env": {**os.environ.copy(), **EXTRA_ENV}}
        nice_run(["terraform", "state", "rm", resource_address], **kwargs)

    @classmethod
    def verify_storage(cls, layer: "Layer") -> bool:
        if layer.cloud == "aws":
            return cls._aws_verify_storage(layer)
        elif layer.cloud == "google":
            return cls._gcp_verify_storage(layer)
        elif layer.cloud == "azurerm":
            return cls._azure_verify_storage(layer)
        else:
            raise Exception(f"Can not verify state storage for cloud {layer.cloud}")

    @classmethod
    def _azure_verify_storage(cls, layer: "Layer") -> bool:
        credentials = Azure.get_credentials()
        providers = layer.gen_providers(0)

        resource_group_name = providers["terraform"]["backend"]["azurerm"][
            "resource_group_name"
        ]
        storage_account_name = providers["terraform"]["backend"]["azurerm"][
            "storage_account_name"
        ]
        container_name = providers["terraform"]["backend"]["azurerm"]["container_name"]
        subscription_id = providers["provider"]["azurerm"]["subscription_id"]

        storage_client = StorageManagementClient(credentials, subscription_id)
        try:
            storage_client.blob_containers.get(
                resource_group_name, storage_account_name, container_name
            )
            return True
        except ResourceNotFoundError:
            return False

    @classmethod
    def _gcp_verify_storage(cls, layer: "Layer") -> bool:
        credentials, project_id = GCP.get_credentials()
        bucket = layer.state_storage()
        gcs_client = storage.Client(project=project_id, credentials=credentials)
        try:
            gcs_client.get_bucket(bucket)
        except NotFound:
            return False
        return True

    @classmethod
    def _aws_verify_storage(cls, layer: "Layer") -> bool:
        bucket = layer.state_storage()
        s3 = boto3.client("s3")
        try:
            s3.get_bucket_encryption(Bucket=bucket,)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchBucket":
                return False
            raise e
        return True

    @classmethod
    def plan(cls, *tf_flags: str, quiet: Optional[bool] = False) -> None:
        cls.init(quiet)
        kwargs: Dict[str, Any] = {"env": {**os.environ.copy(), **EXTRA_ENV}}
        if quiet:
            kwargs["stderr"] = PIPE
            kwargs["stdout"] = DEVNULL
        nice_run(
            ["terraform", "plan", "-compact-warnings", *tf_flags], check=True, **kwargs
        )

    @classmethod
    def show_plan(cls) -> Dict[Any, Any]:
        out = nice_run(
            ["terraform", "show", "-no-color", "-json", "tf.plan"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
        return json.loads(out)

    @classmethod
    def show(cls, *tf_flags: str) -> None:
        kwargs: Dict[str, Any] = {"env": {**os.environ.copy(), **EXTRA_ENV}}
        nice_run(["terraform", "show", *tf_flags], check=True, **kwargs)

    @classmethod
    def get_existing_modules(cls, layer: "Layer") -> Set[str]:
        existing_resources = cls.get_existing_module_resources(layer)
        return set(map(lambda r: r.split(".")[1], existing_resources))

    @classmethod
    def get_existing_module_resources(cls, layer: "Layer") -> List[str]:
        try:
            state = cls.get_state(layer)
        except MissingState:
            logger.info(
                "Could not fetch remote terraform state, assuming no resources exist yet."
            )
            return []
        resources = state.get("resources", [])
        module_resources: List[str] = []
        resource: dict
        for resource in resources:
            if (
                "module" not in resource
                or "type" not in resource
                or "name" not in resource
            ):
                continue
            resource_name_builder = list()
            resource_name_builder.append(resource["module"])
            if resource["mode"] == "managed":
                resource_name_builder.append("data")
            resource_name_builder.append(resource["type"])
            resource_name_builder.append(resource["name"])
            module_resources.append(".".join(resource_name_builder))

        return module_resources

    @classmethod
    def download_state(cls, layer: "Layer") -> bool:
        if not cls.verify_storage(layer):
            logger.info(
                fmt_msg(
                    """
                    We store state in S3/GCP buckets/Azure Storage. Since the state bucket was not found,
                    ~this probably means that you either haven't created your opta resources yet,
                    ~or you previously successfully destroyed your opta resources.
                    """
                )
            )
            return False

        state_file: str = "./tmp.tfstate"
        providers = layer.gen_providers(0)
        if "s3" in providers.get("terraform", {}).get("backend", {}):
            bucket = providers["terraform"]["backend"]["s3"]["bucket"]
            key = providers["terraform"]["backend"]["s3"]["key"]
            logger.debug(
                f"Found an s3 backend in bucket {bucket} and key {key}, "
                "gonna try to download the statefile from there"
            )
            s3 = boto3.client("s3")
            try:
                s3.download_file(Bucket=bucket, Key=key, Filename=state_file)
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    # The object does not exist.
                    logger.debug("Did not find terraform state file")
                    return False
                raise
        elif "gcs" in providers.get("terraform", {}).get("backend", {}):
            bucket = providers["terraform"]["backend"]["gcs"]["bucket"]
            prefix = providers["terraform"]["backend"]["gcs"]["prefix"]
            credentials, project_id = GCP.get_credentials()
            gcs_client = storage.Client(project=project_id, credentials=credentials)
            bucket_object = gcs_client.get_bucket(bucket)
            blob = storage.Blob(f"{prefix}/default.tfstate", bucket_object)
            try:
                with open(state_file, "wb") as file_obj:
                    gcs_client.download_blob_to_file(blob, file_obj)
            except GoogleClientError as e:
                if e.code == 404:
                    # The object does not exist.
                    os.remove(state_file)
                    return False
                raise
        elif "azurerm" in providers.get("terraform", {}).get("backend", {}):
            storage_account_name = providers["terraform"]["backend"]["azurerm"][
                "storage_account_name"
            ]
            container_name = providers["terraform"]["backend"]["azurerm"][
                "container_name"
            ]
            key = providers["terraform"]["backend"]["azurerm"]["key"]

            credentials = Azure.get_credentials()
            try:
                blob = (
                    BlobServiceClient(
                        f"https://{storage_account_name}.blob.core.windows.net/",
                        credential=credentials,
                    )
                    .get_container_client(container_name)
                    .get_blob_client(key)
                )
                with open(state_file, "wb") as file_obj:
                    blob_data = blob.download_blob()
                    blob_data.readinto(file_obj)
            except ResourceNotFoundError:
                return False
        else:
            raise UserErrors("Need to get state from S3 or GCS or Azure storage")

        with open(state_file, "r") as file:
            raw_state = file.read().strip()
        os.remove(state_file)
        if raw_state != "":
            cls.downloaded_state[layer.name] = json.loads(raw_state)
            return True
        return False

    @classmethod
    def _aws_delete_state_storage(cls, layer: "Layer") -> None:
        providers = layer.gen_providers(0)
        if "s3" not in providers.get("terraform", {}).get("backend", {}):
            return

        # Delete the state storage bucket
        bucket_name = providers["terraform"]["backend"]["s3"]["bucket"]
        AWS.delete_bucket(bucket_name)

        # Delete the dynamodb state lock table
        dynamodb_table = providers["terraform"]["backend"]["s3"]["dynamodb_table"]
        region = providers["terraform"]["backend"]["s3"]["region"]
        AWS.delete_dynamodb_table(dynamodb_table, region)
        logger.info("Successfully deleted AWS state storage")

    @classmethod
    def _gcp_delete_state_storage(cls, layer: "Layer") -> None:
        providers = layer.gen_providers(0)
        if "gcs" not in providers.get("terraform", {}).get("backend", {}):
            return
        bucket_name = providers["terraform"]["backend"]["gcs"]["bucket"]
        credentials, project_id = GCP.get_credentials()
        gcs_client = storage.Client(project=project_id, credentials=credentials)
        try:
            bucket_obj = gcs_client.get_bucket(bucket_name)
            bucket_obj.delete(force=True)
            logger.info("Successfully deleted GCP state storage")
        except NotFound:
            logger.warn("State bucket was already deleted")

    @classmethod
    def _create_azure_state_storage(cls, providers: dict) -> None:
        resource_group_name = providers["terraform"]["backend"]["azurerm"][
            "resource_group_name"
        ]

        region = providers["provider"]["azurerm"]["location"]
        subscription_id = providers["provider"]["azurerm"]["subscription_id"]
        storage_account_name = providers["terraform"]["backend"]["azurerm"][
            "storage_account_name"
        ]
        container_name = providers["terraform"]["backend"]["azurerm"]["container_name"]

        # Create RG
        credential = Azure.get_credentials()
        resource_client = ResourceManagementClient(credential, subscription_id)
        try:
            rg_result = resource_client.resource_groups.create_or_update(
                resource_group_name, {"location": region}
            )
        except ResourceNotFoundError as e:
            if "SubscriptionNotFound" in e.message:
                raise UserErrors(
                    f"SubscriptionId {subscription_id} does not exists. Please check and use the correct Subscription Id. "
                    "This is used for accessing the resources in the Resource Group."
                )
        except HttpResponseError as e:
            if "InvalidSubscriptionId" in e.message:
                raise UserErrors(
                    f"Malformed or Invalid SubscriptionId {subscription_id} used. Please check and use the correct Subscription Id. "
                    "This is used for accessing the resources in the Resource Group."
                )

        print(
            f"Provisioned resource group {rg_result.name} in the {rg_result.location} region"
        )
        authorization_client = AuthorizationManagementClient(
            credential, subscription_id, api_version="2018-01-01-preview"
        )

        owner_role_name = "Owner"
        owner_role = list(
            authorization_client.role_definitions.list(
                rg_result.id, filter="roleName eq '{}'".format(owner_role_name)
            )
        )[0]

        storage_role_name = "Storage Blob Data Owner"
        storage_role = list(
            authorization_client.role_definitions.list(
                rg_result.id, filter="roleName eq '{}'".format(storage_role_name)
            )
        )[0]

        key_vault_role_name = "Key Vault Administrator"
        key_vault_role = list(
            authorization_client.role_definitions.list(
                rg_result.id, filter="roleName eq '{}'".format(key_vault_role_name)
            )
        )[0]

        role_assignments = authorization_client.role_assignments.list_for_resource_group(
            rg_result.name
        )
        for role_assignment in role_assignments:
            if role_assignment.role_definition_id == owner_role.id:
                try:
                    authorization_client.role_assignments.create(
                        scope=f"/subscriptions/{subscription_id}/resourceGroups/{rg_result.name}",
                        role_assignment_name=uuid4(),
                        parameters={
                            "role_definition_id": storage_role.id,
                            "principal_id": role_assignment.principal_id,
                        },
                    )
                except ResourceExistsError:
                    pass
                try:
                    authorization_client.role_assignments.create(
                        scope=f"/subscriptions/{subscription_id}/resourceGroups/{rg_result.name}",
                        role_assignment_name=uuid4(),
                        parameters={
                            "role_definition_id": key_vault_role.id,
                            "principal_id": role_assignment.principal_id,
                        },
                    )
                except ResourceExistsError:
                    pass

        # Create SA
        storage_client = StorageManagementClient(credential, subscription_id)
        try:
            storage_client.storage_accounts.get_properties(
                resource_group_name, storage_account_name
            )
            print(f"Storage account {storage_account_name} already exists!")
        except ResourceNotFoundError:
            print("Need to create storage account")
            # create sa
            try:
                poller = storage_client.storage_accounts.begin_create(
                    resource_group_name,
                    storage_account_name,
                    {
                        "location": region,
                        "kind": "StorageV2",
                        "sku": {"name": "Standard_LRS"},
                    },
                )
            except ResourceExistsError:
                raise UserErrors(
                    "The Storage Account name already exists in Another Subscription. "
                    "Please change the Name or Org Name in Config."
                )

            account_result = poller.result()
            print(f"Provisioned storage account {account_result.name}")
            # TODO(ankur): assign Storage Blob Data Contributor to this SA,
            # otherwise it doesn't work

        # create container
        try:
            container = storage_client.blob_containers.get(
                resource_group_name, storage_account_name, container_name
            )
            print(f"container {container.name} exists")
        except ResourceNotFoundError:
            print("Need to create container")
            container = storage_client.blob_containers.create(
                resource_group_name, storage_account_name, container_name, {}
            )
            print(f"Provisioned container {container.name}")

    @classmethod
    def _create_gcp_state_storage(cls, providers: dict) -> None:
        bucket_name = providers["terraform"]["backend"]["gcs"]["bucket"]
        region = providers["provider"]["google"]["region"]
        project_name = providers["provider"]["google"]["project"]
        credentials, project_id = GCP.get_credentials()
        if project_id != project_name:
            raise UserErrors(
                f"We got {project_name} as the project name in opta, but {project_id} in the google credentials"
            )
        gcs_client = storage.Client(project=project_id, credentials=credentials)
        try:
            bucket = gcs_client.get_bucket(bucket_name)
            bucket_project_number = bucket.project_number
        except GoogleClientError as e:
            if e.code == 403:
                raise UserErrors(
                    f"The Bucket Name: {bucket_name} (Opta needs to store state here) already exists.\n"
                    "Possible Failures:\n"
                    " - Bucket is present in some other project and User does not have access to the Project.\n"
                    "Please change the name in the Opta Configuration file or please change the User Permissions.\n"
                    "Please fix it and try again."
                )
            elif e.code != 404:
                raise UserErrors(
                    "When trying to determine the status of the state bucket, we got an "
                    f"{e.code} error with the message "
                    f"{e.message}"
                )
            logger.info("GCS bucket for terraform state not found, creating a new one")
            try:
                bucket = gcs_client.create_bucket(bucket_name, location=region)
                bucket_project_number = bucket.project_number
            except Conflict:
                raise UserErrors(
                    f"It looks like a gcs bucket with the name {bucket_name} was created recently, but then deleted "
                    "and Google keeps hold of gcs bucket names for 30 days after deletion-- pls wait until the end of "
                    "that time or change your environment name slightly."
                )

        # Enable the APIs
        credentials = GoogleCredentials.get_application_default()
        service = discovery.build(
            "serviceusage", "v1", credentials=credentials, static_discovery=False
        )
        new_api_enabled = False
        for service_name in [
            "container.googleapis.com",
            "iam.googleapis.com",
            "containerregistry.googleapis.com",
            "cloudkms.googleapis.com",
            "dns.googleapis.com",
            "servicenetworking.googleapis.com",
            "redis.googleapis.com",
            "compute.googleapis.com",
            "secretmanager.googleapis.com",
            "cloudresourcemanager.googleapis.com",
        ]:
            request = service.services().enable(
                name=f"projects/{project_name}/services/{service_name}"
            )
            try:
                response = request.execute()
                new_api_enabled = new_api_enabled or (
                    response.get("name") != "operations/noop.DONE_OPERATION"
                )
            except HttpError as e:
                if e.resp.status == 400:
                    raise UserErrors(
                        f"Got a 400 response when trying to enable the google {service_name} service with the following error reason: {e._get_reason()}"
                    )
            print(f"Google service {service_name} activated")
        if new_api_enabled:
            logger.info(
                "New api has been enabled, waiting 120 seconds before progressing"
            )
            time.sleep(120)
        else:
            logger.info("No new API found that needs to be enabled")
        service = discovery.build(
            "cloudresourcemanager", "v1", credentials=credentials, static_discovery=False,
        )
        request = service.projects().get(projectId=project_id)
        response = request.execute()

        if response["projectNumber"] != str(bucket_project_number):
            raise UserErrors(
                f"State storage bucket {bucket_name}, has already been created, but it was created in another project. "
                f"Current project's number {response['projectNumber']}. Bucket's project number: {bucket_project_number}. "
                "You do, however, have access to view that bucket, so it sounds like you already run this opta apply in "
                "your org, but on a different project."
                "Note: project number is NOT project id. It is yet another globally unique identifier for your project "
                "I kid you not, go ahead and look it up."
            )

    @classmethod
    def _create_aws_state_storage(cls, providers: dict) -> None:
        bucket_name = providers["terraform"]["backend"]["s3"]["bucket"]
        dynamodb_table = providers["terraform"]["backend"]["s3"]["dynamodb_table"]
        region = providers["terraform"]["backend"]["s3"]["region"]
        s3 = boto3.client("s3")
        dynamodb = boto3.client("dynamodb", config=Config(region_name=region))
        iam = boto3.client("iam", config=Config(region_name=region))
        try:
            s3.get_bucket_encryption(Bucket=bucket_name,)
        except ClientError as e:
            if e.response["Error"]["Code"] == "AuthFailure":
                raise UserErrors(
                    "The AWS Credentials are not configured properly.\n"
                    "Visit https://docs.aws.amazon.com/sdk-for-java/v1/developer-guide/setup-credentials.html "
                    "for more information."
                )
            if e.response["Error"]["Code"] == "AccessDenied":
                raise UserErrors(
                    f"We were unable to access the S3 bucket, {bucket_name} on your AWS account (opta needs this to store state).\n"
                    "Possible Issues: \n"
                    " - Bucket name is not unique and might be present in some other Account. Try updating the name in Configuration file to something else.\n"
                    " - It could also mean that your AWS account has insufficient permissions.\n"
                    "Please fix these issues and try again!"
                )
            if e.response["Error"]["Code"] != "NoSuchBucket":
                raise UserErrors(
                    "When trying to determine the status of the state bucket, we got an "
                    f"{e.response['Error']['Code']} error with the message "
                    f"{e.response['Error']['Message']}"
                )
            logger.info("S3 bucket for terraform state not found, creating a new one")
            if region == "us-east-1":
                s3.create_bucket(Bucket=bucket_name,)
            else:
                s3.create_bucket(
                    Bucket=bucket_name,
                    CreateBucketConfiguration={"LocationConstraint": region},
                )
            s3.put_bucket_encryption(
                Bucket=bucket_name,
                ServerSideEncryptionConfiguration={
                    "Rules": [
                        {
                            "ApplyServerSideEncryptionByDefault": {
                                "SSEAlgorithm": "AES256"
                            },
                        },
                    ]
                },
            )
            s3.put_public_access_block(
                Bucket=bucket_name,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )
            s3.put_bucket_versioning(
                Bucket=bucket_name, VersioningConfiguration={"Status": "Enabled"},
            )
            s3.put_bucket_lifecycle(
                Bucket=bucket_name,
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": "default",
                            "Prefix": "/",
                            "Status": "Enabled",
                            "NoncurrentVersionTransition": {
                                "NoncurrentDays": 30,
                                "StorageClass": "GLACIER",
                            },
                            "NoncurrentVersionExpiration": {"NoncurrentDays": 60},
                            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 10},
                        },
                    ]
                },
            )

        try:
            dynamodb.describe_table(TableName=dynamodb_table)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise UserErrors(
                    "When trying to determine the status of the state dynamodb table, we got an "
                    f"{e.response['Error']['Code']} error with the message "
                    f"{e.response['Error']['Message']}"
                )
            print("Dynamodb table for terraform state not found, creating a new one")
            dynamodb.create_table(
                TableName=dynamodb_table,
                KeySchema=[{"AttributeName": "LockID", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "LockID", "AttributeType": "S"}],
                BillingMode="PROVISIONED",
                ProvisionedThroughput={
                    "ReadCapacityUnits": 20,
                    "WriteCapacityUnits": 20,
                },
            )
        # Create the service linked roles
        try:
            iam.create_service_linked_role(AWSServiceName="autoscaling.amazonaws.com",)
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidInput":
                raise UserErrors(
                    "When trying to create the aws service linked role for autoscaling, we got an "
                    f"{e.response['Error']['Code']} error with the message "
                    f"{e.response['Error']['Message']}"
                )
            logger.debug("Autoscaling service linked role present")
        try:
            iam.create_service_linked_role(
                AWSServiceName="elasticloadbalancing.amazonaws.com",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidInput":
                raise UserErrors(
                    "When trying to create the aws service linked role for load balancing, we got an "
                    f"{e.response['Error']['Code']} error with the message "
                    f"{e.response['Error']['Message']}"
                )
            logger.debug("Load balancing service linked role present")

    @classmethod
    def create_state_storage(cls, layer: "Layer") -> None:
        """
        Idempotently create remote storage for tf state
        """
        providers = layer.gen_providers(0, clean=False)
        if "s3" in providers.get("terraform", {}).get("backend", {}):
            cls._create_aws_state_storage(providers)
        if "gcs" in providers.get("terraform", {}).get("backend", {}):
            cls._create_gcp_state_storage(providers)
        if "azurerm" in providers.get("terraform", {}).get("backend", {}):
            cls._create_azure_state_storage(providers)


def get_terraform_outputs(layer: "Layer") -> dict:
    """ Fetch terraform outputs from existing TF file """
    current_outputs = Terraform.get_outputs(layer)
    parent_outputs = _fetch_parent_outputs(layer)
    return deep_merge(current_outputs, parent_outputs)


def _fetch_parent_outputs(layer: "Layer") -> dict:
    # Fetch the terraform state
    state = Terraform.get_state(layer)

    # Fetch any parent remote states
    resources = state.get("resources", [])
    parent_states = [
        resource
        for resource in resources
        if resource.get("type") == "terraform_remote_state"
    ]

    # Grab all outputs from each remote state and save it.
    parent_state_outputs = {}
    for parent in parent_states:
        parent_outputs = (
            parent["instances"][0]
            .get("attributes", {})
            .get("outputs", {})
            .get("value", {})
        )
        for k, v in parent_outputs.items():
            parent_name = parent.get("name")
            output_name = f"{parent_name}.{k}"
            parent_state_outputs[output_name] = v

    return parent_state_outputs


def fetch_terraform_state_resources(layer: "Layer") -> dict:
    Terraform.download_state(layer)
    state = Terraform.get_state(layer)

    resources = state.get("resources", [])

    resources_dict: Dict[str, dict] = {}
    for resource in resources:
        # Note that resource addresses should start with "module.", but in the
        # saved terraform state, it is already part of the module name.
        # Ex. "module.awsbase"
        address_parts = []
        if resource.get("module"):
            address_parts.append(resource.get("module"))
        if resource.get("mode") == "data":
            address_parts.append(resource.get("mode"))
        address_parts.append(resource.get("type", ""))
        address_parts.append(resource.get("name", ""))
        address = ".".join(address_parts)
        if address == "..":
            continue

        # Some resources like module.awsdns.aws_acm_certificate.certificate have
        # an empty instances list.
        if len(resource["instances"]) == 0:
            resources_dict[address] = {}
        else:
            resources_dict[address] = resource["instances"][0]["attributes"]

        resources_dict[address]["module"] = resource.get("module", "")
        resources_dict[address]["type"] = resource.get("type", "")
        resources_dict[address]["name"] = resource.get("name", "")

    return resources_dict
