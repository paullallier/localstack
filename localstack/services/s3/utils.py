import datetime
import re
from typing import Dict, Union

import moto.s3.models as moto_s3_models
from botocore.exceptions import ClientError
from botocore.utils import InvalidArnException
from moto.s3.exceptions import MissingBucket
from moto.s3.models import FakeBucket, FakeDeleteMarker, FakeKey

from localstack.aws.api import CommonServiceException, ServiceException
from localstack.aws.api.s3 import (
    BucketCannedACL,
    BucketName,
    ChecksumAlgorithm,
    InvalidArgument,
    NoSuchBucket,
    NoSuchKey,
    ObjectCannedACL,
    ObjectKey,
    Permission,
    StorageClass,
)
from localstack.utils.aws import arns, aws_stack
from localstack.utils.aws.arns import parse_arn
from localstack.utils.strings import checksum_crc32, checksum_crc32c, hash_sha1, hash_sha256

checksum_keys = ["ChecksumSHA1", "ChecksumSHA256", "ChecksumCRC32", "ChecksumCRC32C"]

BUCKET_NAME_REGEX = (
    r"(?=^.{3,63}$)(?!^(\d+\.)+\d+$)"
    + r"(^(([a-z0-9]|[a-z0-9][a-z0-9\-]*[a-z0-9])\.)*([a-z0-9]|[a-z0-9][a-z0-9\-]*[a-z0-9])$)"
)

REGION_REGEX = r"[a-z]{2}-[a-z]+-[0-9]{1,}"
PORT_REGEX = r"(:[\d]{0,6})?"

S3_VIRTUAL_HOSTNAME_REGEX = (  # path based refs have at least valid bucket expression (separated by .) followed by .s3
    r"^(http(s)?://)?((?!s3\.)[^\./]+)\."  # the negative lookahead part is for considering buckets
    r"(((s3(-website)?\.({}\.)?)localhost(\.localstack\.cloud)?)|(localhost\.localstack\.cloud)|"
    r"(s3((-website)|(-external-1))?[\.-](dualstack\.)?"
    r"({}\.)?amazonaws\.com(.cn)?)){}(/[\w\-. ]*)*$"
).format(
    REGION_REGEX, REGION_REGEX, PORT_REGEX
)

PATTERN_UUID = re.compile(
    r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"
)

S3_VIRTUAL_HOST_FORWARDED_HEADER = "x-s3-vhost-forwarded-for"

VALID_CANNED_ACLS_BUCKET = {
    # https://docs.aws.amazon.com/AmazonS3/latest/userguide/acl-overview.html#canned-acl
    # bucket-owner-read + bucket-owner-full-control are allowed, but ignored for buckets
    ObjectCannedACL.private,
    ObjectCannedACL.authenticated_read,
    ObjectCannedACL.aws_exec_read,
    ObjectCannedACL.bucket_owner_full_control,
    ObjectCannedACL.bucket_owner_read,
    ObjectCannedACL.public_read,
    ObjectCannedACL.public_read_write,
    BucketCannedACL.log_delivery_write,
}

VALID_ACL_PREDEFINED_GROUPS = {
    "http://acs.amazonaws.com/groups/global/AuthenticatedUsers",
    "http://acs.amazonaws.com/groups/global/AllUsers",
    "http://acs.amazonaws.com/groups/s3/LogDelivery",
}

VALID_GRANTEE_PERMISSIONS = {
    Permission.FULL_CONTROL,
    Permission.READ,
    Permission.READ_ACP,
    Permission.WRITE,
    Permission.WRITE_ACP,
}

VALID_STORAGE_CLASSES = [
    StorageClass.STANDARD,
    StorageClass.STANDARD_IA,
    StorageClass.GLACIER,
    StorageClass.GLACIER_IR,
    StorageClass.REDUCED_REDUNDANCY,
    StorageClass.ONEZONE_IA,
    StorageClass.INTELLIGENT_TIERING,
    StorageClass.DEEP_ARCHIVE,
]

# response header overrides the client may request
ALLOWED_HEADER_OVERRIDES = {
    "ResponseContentType": "ContentType",
    "ResponseContentLanguage": "ContentLanguage",
    "ResponseExpires": "Expires",
    "ResponseCacheControl": "CacheControl",
    "ResponseContentDisposition": "ContentDisposition",
    "ResponseContentEncoding": "ContentEncoding",
}


class InvalidRequest(ServiceException):
    code: str = "InvalidRequest"
    sender_fault: bool = False
    status_code: int = 400


def get_object_checksum_for_algorithm(checksum_algorithm: str, data: bytes):
    match checksum_algorithm:
        case ChecksumAlgorithm.CRC32:
            return checksum_crc32(data)

        case ChecksumAlgorithm.CRC32C:
            return checksum_crc32c(data)

        case ChecksumAlgorithm.SHA1:
            return hash_sha1(data)

        case ChecksumAlgorithm.SHA256:
            return hash_sha256(data)

        case _:
            # TODO: check proper error? for now validated client side, need to check server response
            raise InvalidRequest("The value specified in the x-amz-trailer header is not supported")


def verify_checksum(checksum_algorithm: str, data: bytes, request: Dict):
    # TODO: you don't have to specify the checksum algorithm
    # you can use only the checksum-{algorithm-type} header
    # https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity.html
    key = f"Checksum{checksum_algorithm.upper()}"
    # TODO: is there a message if the header is missing?
    checksum = request.get(key)
    calculated_checksum = get_object_checksum_for_algorithm(checksum_algorithm, data)

    if calculated_checksum != checksum:
        raise InvalidRequest(
            f"Value for x-amz-checksum-{checksum_algorithm.lower()} header is invalid."
        )


def is_key_expired(key_object: Union[FakeKey, FakeDeleteMarker]) -> bool:
    if not key_object or isinstance(key_object, FakeDeleteMarker) or not key_object._expiry:
        return False
    return key_object._expiry <= datetime.datetime.now(key_object._expiry.tzinfo)


def is_bucket_name_valid(bucket_name: str) -> bool:
    """
    ref. https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html
    """
    return True if re.match(BUCKET_NAME_REGEX, bucket_name) else False


def is_canned_acl_bucket_valid(canned_acl: str) -> bool:
    return canned_acl in VALID_CANNED_ACLS_BUCKET


def get_header_name(capitalized_field: str) -> str:
    headers_parts = re.split(r"([A-Z][a-z]+)", capitalized_field)
    return f"x-amz-{'-'.join([part.lower() for part in headers_parts if part])}"


def is_valid_canonical_id(canonical_id: str) -> bool:
    """
    Validate that the string is a hex string with 64 char
    """
    try:
        return int(canonical_id, 16) and len(canonical_id) == 64
    except ValueError:
        return False


def forwarded_from_virtual_host_addressed_request(headers: Dict[str, str]) -> bool:
    """
    Determines if the request was forwarded from a v-host addressing style into a path one
    """
    # we can assume that the host header we are receiving here is actually the header we originally received
    # from the client (because the edge service is forwarding the request in memory)
    match = re.match(S3_VIRTUAL_HOSTNAME_REGEX, headers.get(S3_VIRTUAL_HOST_FORWARDED_HEADER, ""))

    # checks whether there is a bucket name. This is sort of hacky
    return True if match and match.group(3) else False


def get_bucket_from_moto(
    moto_backend: moto_s3_models.S3Backend, bucket: BucketName
) -> moto_s3_models.FakeBucket:
    # TODO: check authorization for buckets as well?
    try:
        return moto_backend.get_bucket(bucket_name=bucket)
    except MissingBucket:
        ex = NoSuchBucket("The specified bucket does not exist")
        ex.BucketName = bucket
        raise ex


def get_key_from_moto_bucket(
    moto_bucket: moto_s3_models.FakeBucket, key: ObjectKey
) -> moto_s3_models.FakeKey:
    fake_key = moto_bucket.keys.get(key)
    if not fake_key:
        ex = NoSuchKey("The specified key does not exist.")
        ex.Key = key
        raise ex

    return fake_key


def _create_invalid_argument_exc(
    message: Union[str, None], name: str, value: str, host_id: str = None
) -> InvalidArgument:
    ex = InvalidArgument(message)
    ex.ArgumentName = name
    ex.ArgumentValue = value
    if host_id:
        ex.HostId = host_id
    return ex


def capitalize_header_name_from_snake_case(header_name: str) -> str:
    return "-".join([part.capitalize() for part in header_name.split("-")])


def validate_kms_key_id(kms_key: str, bucket: FakeBucket):
    """
    Validate that the KMS key used to encrypt the object is valid
    :param kms_key: the KMS key id or ARN
    :param bucket: the targeted bucket
    :raise
    :return:
    """
    try:
        parsed_arn = parse_arn(kms_key)
        key_region = parsed_arn["region"]
        # the KMS key should be in the same region as the bucket, we can raise an exception without calling KMS
        if key_region != bucket.region_name:
            raise CommonServiceException(
                code="KMS.NotFoundException", message=f"Invalid arn {key_region}"
            )

    except InvalidArnException:
        # if it fails, the passed ID is a UUID with no region data
        key_id = kms_key
        # recreate the ARN manually with the bucket region and bucket owner
        # if the KMS key is cross-account, user should provide an ARN and not a KeyId
        kms_key = arns.kms_key_arn(
            key_id=key_id, account_id=bucket.account_id, region_name=bucket.region_name
        )

    # the KMS key should be in the same region as the bucket, create the client in the bucket region
    kms_client = aws_stack.connect_to_service("kms", region_name=bucket.region_name)
    try:
        kms_client.describe_key(KeyId=kms_key)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NotFoundException":
            raise CommonServiceException(
                code="KMS.NotFoundException", message=e.response["Error"]["Message"]
            )
        raise
