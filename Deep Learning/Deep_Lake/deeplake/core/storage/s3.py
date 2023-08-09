import deeplake
from math import ceil
import time
import boto3
import botocore  # type: ignore
import posixpath
from typing import Dict, Optional, Tuple, Type
from datetime import datetime, timezone
from botocore.session import ComponentLocator
from deeplake.client.client import DeepLakeBackendClient
from deeplake.core.storage.provider import StorageProvider
from deeplake.util.exceptions import (
    S3GetAccessError,
    S3DeletionError,
    S3GetError,
    S3SetError,
    S3Error,
    PathNotEmptyException,
)
from deeplake.util.warnings import always_warn
from botocore.exceptions import (
    ReadTimeoutError,
    ConnectionError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    IncompleteReadError,
)

CONNECTION_ERRORS = (
    ReadTimeoutError,
    ConnectionError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    IncompleteReadError,
)

try:
    from botocore.exceptions import ResponseStreamingError

    CONNECTION_ERRORS = CONNECTION_ERRORS + (ResponseStreamingError,)  # type: ignore
except ImportError:
    pass

try:
    import aioboto3  # type: ignore
    import asyncio  # type: ignore
    import nest_asyncio  # type: ignore

    nest_asyncio.apply()  # needed to run asyncio in jupyter notebook
except Exception:
    aioboto3 = None  # type: ignore
    asyncio = None  # type: ignore


class S3ResetReloadCredentialsManager:
    """Tries to reload the credentials if the error is due to expired token, if error still occurs, it raises it."""

    def __init__(self, s3p, error_class: Type[S3Error]):
        self.error_class = error_class
        self.s3p = s3p

    def __enter__(self):
        if self.s3p.expiration:
            self.s3p._check_update_creds(force=True)
        else:
            self.s3p._locate_and_load_creds()
            self.s3p._set_s3_client_and_resource()
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if exc_type is not None:
            raise self.error_class(exc_value).with_traceback(exc_traceback)


class S3Provider(StorageProvider):
    """Provider class for using S3 storage."""

    def __init__(
        self,
        root: str,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        aws_region: Optional[str] = None,
        profile_name: Optional[str] = None,
        token: Optional[str] = None,
        **kwargs,
    ):
        """Initializes the S3Provider

        Example:

            >>> s3_provider = S3Provider("snark-test/benchmarks")

        Args:
            root (str): The root of the provider. All read/write request keys will be appended to root.
            aws_access_key_id (str, optional): Specifies the AWS access key used as part of the credentials to
                authenticate the user.
            aws_secret_access_key (str, optional): Specifies the AWS secret key used as part of the credentials to
                authenticate the user.
            aws_session_token (str, optional): Specifies an AWS session token used as part of the credentials to
                authenticate the user.
            endpoint_url (str, optional): The complete URL to use for the constructed client.
                This needs to be provided for cases in which you're interacting with MinIO, Wasabi, etc.
            aws_region (str, optional): Specifies the AWS Region to send requests to.
            profile_name (str, optional): Specifies the AWS profile name to use.
            token (str, optional): Activeloop token, used for fetching credentials for Deep Lake datasets (if this is underlying storage for Deep Lake dataset).
                This is optional, tokens are normally autogenerated.
            **kwargs: Additional arguments to pass to the S3 client. Includes: ``expiration``.
        """
        self.root = root
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.aws_session_token = aws_session_token
        self.aws_region: Optional[str] = aws_region
        self.endpoint_url: Optional[str] = endpoint_url
        self.expiration: Optional[str] = None
        self.repository: Optional[str] = None
        self.db_engine: bool = False
        self.tag: Optional[str] = None
        self.token: Optional[str] = token
        self.loaded_creds_from_environment = False
        self.client_config = deeplake.config["s3"]
        self.start_time = time.time()
        self.profile_name = profile_name
        self._initialize_s3_parameters()
        self._presigned_urls: Dict[str, Tuple[str, float]] = {}
        self.creds_used: Optional[str] = None

    def async_supported(self) -> bool:
        return asyncio is not None

    def subdir(self, path: str, read_only: bool = False):
        sd = self.__class__(
            root=posixpath.join(self.root, path),
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            aws_session_token=self.aws_session_token,
            aws_region=self.aws_region,
            endpoint_url=self.endpoint_url,
            token=self.token,
        )
        if self.expiration:
            sd._set_hub_creds_info(self.hub_path, self.expiration, self.db_engine, self.repository)  # type: ignore
        sd.read_only = read_only
        sd.creds_used = self.creds_used
        return sd

    def _set(self, path, content):
        self.client.put_object(
            Bucket=self.bucket,
            Body=content,
            Key=path,
            ContentType="application/octet-stream",  # signifies binary data
        )

    def __setitem__(self, path, content):
        """Sets the object present at the path with the value

        Args:
            path (str): the path relative to the root of the S3Provider.
            content (bytes): the value to be assigned at the path.

        Raises:
            S3SetError: Any S3 error encountered while setting the value at the path.
            ReadOnlyError: If the provider is in read-only mode.
        """
        self.check_readonly()
        self._check_update_creds()
        path = "".join((self.path, path))
        content = bytearray(memoryview(content))
        try:
            self._set(path, content)
        except botocore.exceptions.ClientError as err:
            with S3ResetReloadCredentialsManager(self, S3SetError):
                self._set(path, content)
        except CONNECTION_ERRORS as err:
            tries = self.num_tries
            for i in range(1, tries + 1):
                always_warn(f"Encountered connection error, retry {i} out of {tries}")
                try:
                    self._set(path, content)
                    always_warn(
                        f"Connection re-established after {i} {['retries', 'retry'][i==1]}."
                    )
                    return
                except Exception:
                    pass
            raise S3SetError(err) from err
        except Exception as err:
            raise S3SetError(err) from err

    def _get(self, path, bucket=None):
        bucket = bucket or self.bucket
        resp = self.client.get_object(
            Bucket=bucket,
            Key=path,
        )
        return resp["Body"].read()

    def __getitem__(self, path):
        """Gets the object present at the path.

        Args:
            path (str): the path relative to the root of the S3Provider.

        Returns:
            bytes: The bytes of the object present at the path.

        Raises:
            KeyError: If an object is not found at the path.
            S3GetError: Any other error other than KeyError while retrieving the object.
        """
        return self.get_bytes(path)

    def _get_bytes(
        self, path, start_byte: Optional[int] = None, end_byte: Optional[int] = None
    ):
        if start_byte is not None and end_byte is not None:
            if start_byte == end_byte:
                return b""
            range = f"bytes={start_byte}-{end_byte - 1}"
        elif start_byte is not None:
            range = f"bytes={start_byte}-"
        elif end_byte is not None:
            range = f"bytes=0-{end_byte - 1}"
        else:
            range = ""
        resp = self.client.get_object(Bucket=self.bucket, Key=path, Range=range)
        return resp["Body"].read()

    def get_bytes(
        self,
        path: str,
        start_byte: Optional[int] = None,
        end_byte: Optional[int] = None,
    ):
        """Gets the object present at the path within the given byte range.

        Args:
            path (str): The path relative to the root of the provider.
            start_byte (int, optional): If only specific bytes starting from ``start_byte`` are required.
            end_byte (int, optional): If only specific bytes up to end_byte are required.

        Returns:
            bytes: The bytes of the object present at the path within the given byte range.

        Raises:
            InvalidBytesRequestedError: If ``start_byte`` > ``end_byte`` or ``start_byte`` < 0 or ``end_byte`` < 0.
            KeyError: If an object is not found at the path.
            S3GetAccessError: Invalid credentials for the object path storage.
            S3GetError: Any other error while retrieving the object.
        """
        self._check_update_creds()
        path = "".join((self.path, path))
        try:
            return self._get_bytes(path, start_byte, end_byte)
        except botocore.exceptions.ClientError as err:
            if err.response["Error"]["Code"] == "NoSuchKey":
                raise KeyError(err) from err
            if err.response["Error"]["Code"] == "InvalidAccessKeyId":
                new_error_cls: Type[S3GetError] = S3GetAccessError
            else:
                new_error_cls = S3GetError
            with S3ResetReloadCredentialsManager(self, new_error_cls):
                return self._get_bytes(path, start_byte, end_byte)
        except CONNECTION_ERRORS as err:
            tries = self.num_tries
            for i in range(1, tries + 1):
                always_warn(f"Encountered connection error, retry {i} out of {tries}")
                try:
                    ret = self._get_bytes(path, start_byte, end_byte)
                    always_warn(
                        f"Connection re-established after {i} {['retries', 'retry'][i==1]}."
                    )
                    return ret
                except Exception:
                    pass
            raise S3GetError(err) from err
        except botocore.exceptions.NoCredentialsError as err:
            raise S3GetAccessError from err
        except Exception as err:
            raise S3GetError(err) from err

    def _del(self, path):
        self.client.delete_object(Bucket=self.bucket, Key=path)

    def __delitem__(self, path):
        """Delete the object present at the path.

        Args:
            path (str): the path to the object relative to the root of the S3Provider.

        Note:
            If the object is not found, s3 won't raise KeyError.

        Raises:
            S3DeletionError: Any S3 error encountered while deleting the object.
            ReadOnlyError: If the provider is in read-only mode.
        """
        self.check_readonly()
        self._check_update_creds()
        path = "".join((self.path, path))
        try:
            self._del(path)
        except botocore.exceptions.ClientError as err:
            with S3ResetReloadCredentialsManager(self, S3DeletionError):
                self._del(path)
        except CONNECTION_ERRORS as err:
            tries = self.num_tries
            for i in range(1, tries + 1):
                always_warn(f"Encountered connection error, retry {i} out of {tries}")
                try:
                    self._del(path)
                    always_warn(
                        f"Connection re-established after {i} {['retries', 'retry'][i==1]}."
                    )
                    return
                except Exception:
                    pass
            raise S3DeletionError(err) from err
        except Exception as err:
            raise S3DeletionError(err) from err

    @property
    def num_tries(self):
        return min(ceil((time.time() - self.start_time) / 300), 5)

    def _keys_iterator(self):
        self._check_update_creds()
        prefix = self.path
        start_after = ""
        prefix = prefix[1:] if prefix.startswith("/") else prefix
        start_after = (start_after or prefix) if prefix.endswith("/") else start_after
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.bucket, Prefix=prefix, StartAfter=start_after
        ):
            for content in page.get("Contents", ()):
                yield content["Key"]

    def _all_keys(self):
        """Helper function that lists all the objects present at the root of the S3Provider.

        Returns:
            set: set of all the objects found at the root of the S3Provider.

        Raises:
            S3ListError: Any S3 error encountered while listing the objects.
        """
        len_path = len(self.path.split("/")) - 1
        return ("/".join(name.split("/")[len_path:]) for name in self._keys_iterator())

    def __len__(self):
        """Returns the number of files present at the root of the S3Provider.

        Note:
            This is an expensive operation.

        Returns:
            int: the number of files present inside the root.

        Raises:
            S3ListError: Any S3 error encountered while listing the objects.
        """
        return sum(1 for _ in self._keys_iterator())

    def __iter__(self):
        """Generator function that iterates over the keys of the S3Provider.

        Yields:
            str: the name of the object that it is iterating over.
        """
        self._check_update_creds()
        yield from self._all_keys()

    def clear(self, prefix=""):
        """Deletes ALL data with keys having given prefix on the s3 bucket (under self.root).

        Warning:
            Exercise caution!
        """
        self.check_readonly()
        self._check_update_creds()
        path = posixpath.join(self.path, prefix) if prefix else self.path
        if self.resource is not None:
            try:
                bucket = self.resource.Bucket(self.bucket)
                bucket.objects.filter(Prefix=path).delete()
            except Exception as err:
                with S3ResetReloadCredentialsManager(self, S3DeletionError):
                    bucket = self.resource.Bucket(self.bucket)
                    bucket.objects.filter(Prefix=self.path).delete()

        else:
            super().clear()

    def rename(self, root):
        """Rename root folder."""
        self.check_readonly()
        self._check_update_creds()
        items = []
        paginator = self.client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=self.bucket, Prefix=self.path)
        for page in pages:
            items.extend(page["Contents"])
        path = root.replace("s3://", "")
        _, new_path = path.split("/", 1)
        try:
            dest_objects = self.client.list_objects_v2(
                Bucket=self.bucket, Prefix=new_path
            )["Contents"]
            for item in dest_objects:
                raise PathNotEmptyException(use_hub=False)
        except KeyError:
            pass
        for item in items:
            old_key = item["Key"]
            copy_source = {"Bucket": self.bucket, "Key": old_key}
            new_key = "/".join([new_path, posixpath.relpath(old_key, self.path)])
            self.client.copy_object(
                CopySource=copy_source, Bucket=self.bucket, Key=new_key
            )
            self.client.delete_object(Bucket=self.bucket, Key=old_key)

        self.root = root
        self.path = new_path
        if not self.path.endswith("/"):
            self.path += "/"

    def _state_keys(self):
        """Keys used to store the state of the provider."""
        return {
            "root",
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_session_token",
            "aws_region",
            "endpoint_url",
            "client_config",
            "expiration",
            "db_engine",
            "repository",
            "tag",
            "token",
            "loaded_creds_from_environment",
            "read_only",
            "profile_name",
            "creds_used",
        }

    def __getstate__(self):
        return {key: getattr(self, key) for key in self._state_keys()}

    def __setstate__(self, state):
        assert set(state.keys()) == self._state_keys()
        self.__dict__.update(state)
        self.start_time = time.time()
        self._initialize_s3_parameters()

    def _set_bucket_and_path(self):
        root = self.root.replace("s3://", "")
        split_root = root.split("/", 1)
        self.bucket = split_root[0]
        if len(split_root) > 1:
            self.path = split_root[1]
        else:
            self.path = ""
        if not self.path.endswith("/"):
            self.path += "/"

    def _set_hub_creds_info(
        self,
        hub_path: str,
        expiration: str,
        db_engine: bool = True,
        repository: Optional[str] = None,
    ):
        """Sets the tag and expiration of the credentials. These are only relevant to datasets using Deep Lake storage.
        This info is used to fetch new credentials when the temporary 12 hour credentials expire.

        Args:
            hub_path (str): The deeplake cloud path to the dataset.
            expiration (str): The time at which the credentials expire.
            db_engine (bool): Whether Activeloop DB Engine enabled.
            repository (str, Optional): Backend repository where the dataset is stored.
        """
        self.hub_path = hub_path
        self.tag = hub_path[6:]  # removing the hub:// part from the path
        self.expiration = expiration
        self.db_engine = db_engine
        self.repository = repository

    def _initialize_s3_parameters(self):
        self._set_bucket_and_path()

        if self.aws_access_key_id is None and self.aws_secret_access_key is None:
            self._locate_and_load_creds()
            self.loaded_creds_from_environment = True

        self._set_s3_client_and_resource()

    def _check_update_creds(self, force=False):
        """If the client has an expiration time, check if creds are expired and fetch new ones.
        This would only happen for datasets stored on Deep Lake storage for which temporary 12 hour credentials are generated.
        """
        if self.expiration and (
            force or float(self.expiration) < datetime.now(timezone.utc).timestamp()
        ):
            client = DeepLakeBackendClient(self.token)
            org_id, ds_name = self.tag.split("/")

            mode = "r" if self.read_only else "a"

            url, creds, mode, expiration, repo = client.get_dataset_credentials(
                org_id,
                ds_name,
                mode,
                {"enabled": self.db_engine},
                True,
            )
            self.expiration = expiration
            self.repository = repo
            self.aws_access_key_id = creds.get("aws_access_key_id")
            self.aws_secret_access_key = creds.get("aws_secret_access_key")
            self.aws_session_token = creds.get("aws_session_token")
            self._set_s3_client_and_resource()

    def _locate_and_load_creds(self):
        session = boto3.session.Session(profile_name=self.profile_name)._session
        component_locator = ComponentLocator()
        component_locator.lazy_register_component(
            "credential_provider", session._create_credential_resolver
        )
        credentials = component_locator.get_component(
            "credential_provider"
        ).load_credentials()
        if credentials is not None:
            self.aws_access_key_id = credentials.access_key
            self.aws_secret_access_key = credentials.secret_key
            self.aws_session_token = credentials.token
            self.aws_region = session._resolve_region_name(
                self.aws_region, self.client_config
            )

    def _set_s3_client_and_resource(self):
        kwargs = self.s3_kwargs
        session = boto3.session.Session(profile_name=self.profile_name)
        self.client = session.client("s3", **kwargs)
        self.resource = session.resource("s3", **kwargs)
        if aioboto3 is not None:
            self.async_session = aioboto3.session.Session(
                profile_name=self.profile_name
            )

    @property
    def s3_kwargs(self):
        return {
            "aws_access_key_id": self.aws_access_key_id,
            "aws_secret_access_key": self.aws_secret_access_key,
            "aws_session_token": self.aws_session_token,
            "region_name": self.aws_region,
            "endpoint_url": self.endpoint_url,
            "config": self.client_config,
        }

    def need_to_reload_creds(self, err: botocore.exceptions.ClientError) -> bool:
        """Checks if the credentials need to be reloaded.
        This happens if the credentials were loaded from the environment and have now expired.
        """
        return (
            err.response["Error"]["Code"] == "ExpiredToken"
            and self.loaded_creds_from_environment
        )

    def get_presigned_url(self, key, full=False):
        self._check_update_creds()
        if full:
            root = key.replace("s3://", "")
            split_root = root.split("/", 1)
            bucket = split_root[0]
            path = split_root[1] if len(split_root) > 1 else ""
        else:
            bucket = self.bucket
            path = "".join((self.path, key))

        url = None
        cached = self._presigned_urls.get(path)
        if cached:
            url, t_store = cached
            t_now = time.time()
            if t_now - t_store > 3200:
                del self._presigned_urls[path]
                url = None

        if url is None:
            if self._is_hub_path:
                client = DeepLakeBackendClient(self.token)
                org_id, ds_name = self.tag.split("/")  # type: ignore
                url = client.get_presigned_url(org_id, ds_name, key)
            else:
                url = self.client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": bucket, "Key": path},
                    ExpiresIn=3600,
                )
            self._presigned_urls[path] = (url, time.time())
        return url

    def get_object_size(self, path: str) -> int:
        path = "".join((self.path, path))
        obj = self.resource.Object(self.bucket, path)
        return obj.content_length

    def get_object_from_full_url(self, url: str):
        root = url.replace("s3://", "")
        split_root = root.split("/", 1)
        bucket = split_root[0]
        path = split_root[1] if len(split_root) > 1 else ""
        try:
            return self._get(path, bucket)
        except botocore.exceptions.ClientError as err:
            if err.response["Error"]["Code"] == "NoSuchKey":
                raise KeyError(err) from err
            with S3ResetReloadCredentialsManager(self, S3GetError):
                return self._get(path, bucket)
        except CONNECTION_ERRORS as err:
            tries = self.num_tries
            for i in range(1, tries + 1):
                always_warn(f"Encountered connection error, retry {i} out of {tries}")
                try:
                    ret = self._get(path, bucket)
                    always_warn(
                        f"Connection re-established after {i} {['retries', 'retry'][i==1]}."
                    )
                    return ret
                except Exception:
                    pass
            raise S3GetError(err) from err
        except Exception as err:
            raise S3GetError(err) from err

    def _set_items(self, items: dict):
        async def set_items_async(items):
            async with self.async_session.client("s3", **self.s3_kwargs) as client:
                tasks = []
                for k, v in items.items():
                    tasks.append(
                        asyncio.ensure_future(
                            client.put_object(
                                Bucket=self.bucket, Key=self.path + k, Body=v
                            )
                        )
                    )
                await asyncio.gather(*tasks)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(set_items_async(items))

    def set_items(self, items: dict):
        try:
            self._set_items(items)
        except botocore.exceptions.ClientError as err:
            with S3ResetReloadCredentialsManager(self, S3SetError):
                self._set_items(items)
        except CONNECTION_ERRORS as err:
            tries = self.num_tries
            for i in range(1, tries + 1):
                always_warn(f"Encountered connection error, retry {i} out of {tries}")
                try:
                    self._set_items(items)
                    always_warn(
                        f"Connection re-established after {i} {['retries', 'retry'][i==1]}."
                    )
                    return
                except Exception:
                    pass
            raise S3SetError(err) from err
        except Exception as err:
            raise S3SetError(err) from err