#!/usr/bin/env python
"""
Simple backup and restore script for Amazon DynamoDB using boto to work similarly to mysqldump.

Suitable for DynamoDB usages of smaller data volume which do not warrant the usage of AWS
Data Pipeline for backup/restores/empty.

dynamodump supports local DynamoDB instances as well (tested with DynamoDB Local).
"""

import argparse
import base64
import boto3
import datetime
import errno
import fnmatch
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import threading
import time
import zipfile
from queue import Queue
from six.moves import input
from urllib.error import URLError, HTTPError
from urllib.request import urlopen


AWS_SLEEP_INTERVAL = 10  # seconds
BATCH_WRITE_SLEEP_INTERVAL = 0.15  # seconds
DATA_DIR = "data"
DATA_DUMP = "dump"
DEFAULT_PREFIX_SEPARATOR = "-"
CURRENT_WORKING_DIR = os.getcwd()
JSON_INDENT = 2
LOCAL_SLEEP_INTERVAL = 1  # seconds
LOG_LEVEL = "INFO"
MAX_BATCH_WRITE = 25  # DynamoDB limit
MAX_NUMBER_BACKUP_WORKERS = 25
MAX_RETRY = 6
METADATA_URL = "http://169.254.169.254/latest/meta-data/"
PAY_PER_REQUEST_BILLING_MODE = "PAY_PER_REQUEST"
PROVISIONED_BILLING_MODE = "PROVISIONED"
RESTORE_WRITE_CAPACITY = 25
RESTORE_READ_CAPACITY = 25
SCHEMA_FILE = "schema.json"
THREAD_START_DELAY = 1  # seconds


def encoder(self, obj):
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()

    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("utf-8")

    return json.JSONEncoder.encoder(self, obj)


json.JSONEncoder.default = encoder


def process_list_type(list):
    for elem in list:
        if "B" in elem:
            elem["B"] = base64.b64decode(elem["B"].encode("utf-8"))


def process_item_types(dct):
    for item in dct["Items"]:
        for key in item:
            val = item[key]
            if "B" in val:
                item[key]["B"] = base64.b64decode(val["B"].encode("utf-8"))
            elif "L" in val:
                process_list_type(val["L"])


def _get_aws_client(
    service: str,
    profile: str = None,
    region: str = None,
    secret_key: str = None,
    access_key: str = None,
    endpoint_url: str = None,
):
    """
    Build connection to some AWS service.
    """

    if region:
        aws_region = region
    else:
        aws_region = os.getenv("AWS_DEFAULT_REGION")

    # Fallback to querying metadata for region
    if not aws_region:
        try:
            azone = (
                urlopen(
                    METADATA_URL + "placement/availability-zone", data=None, timeout=5
                )
                .read()
                .decode()
            )
            aws_region = azone[:-1]
        except HTTPError as e:
            logging.exception(
                "Error determining region used for AWS client.  Typo in code?\n\n"
                + str(e)
            )
            sys.exit(1)
        except URLError:
            logging.exception("Timed out connecting to metadata service.\n\n")
            sys.exit(1)

    if profile:
        session = boto3.Session(
            profile_name=profile,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        client = session.client(service, region_name=aws_region)
    else:
        client = boto3.client(
            service,
            region_name=aws_region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            endpoint_url=endpoint_url,
        )
    return client


def get_table_name_by_tag(profile, region, tag):
    """
    Using provided connection to dynamodb and tag, get all tables that have provided tag

    Profile provided and, if needed, used to build connection to STS.
    """

    matching_tables = []
    all_tables = []
    sts = _get_aws_client(profile=profile, region=region, service="sts")
    dynamo = _get_aws_client(profile=profile, region=region, service="dynamodb")
    account_number = sts.get_caller_identity().get("Account")
    paginator = dynamo.get_paginator(operation_name="list_tables")
    tag_key = tag.split("=")[0]
    tag_value = tag.split("=")[1]

    get_all_tables = paginator.paginate()
    for page in get_all_tables:
        for table in page["TableNames"]:
            all_tables.append(table)
            logging.debug("Found table " + table)

    for table in all_tables:
        table_arn = "arn:aws:dynamodb:{}:{}:table/{}".format(
            region, account_number, table
        )
        table_tags = dynamo.list_tags_of_resource(ResourceArn=table_arn)
        for found_tag in table_tags["Tags"]:
            if found_tag["Key"] == tag_key:
                logging.debug("Checking table " + table + " tag " + found_tag["Key"])
                if found_tag["Value"] == tag_value:
                    matching_tables.append(table)
                    logging.info("Matched table " + table)

    return matching_tables


def do_put_bucket_object(profile, region, bucket, bucket_object):
    """
    Put object into bucket.  Only called if we've also created an archive file with do_archive()

    Bucket must exist prior to running this function.
    profile could be None.
    bucket_object is file to be uploaded
    """

    s3 = _get_aws_client(profile=profile, region=region, service="s3")
    logging.info("Uploading backup to S3 bucket " + bucket)
    try:
        s3.upload_file(
            bucket_object,
            bucket,
            bucket_object,
            ExtraArgs={"ServerSideEncryption": "AES256"},
        )
    except s3.exceptions.ClientError as e:
        logging.exception("Failed to put file to S3 bucket\n\n" + str(e))
        sys.exit(1)


def do_get_s3_archive(profile, region, bucket, table, archive):
    """
    Fetch latest file named filename from S3

    Bucket must exist prior to running this function.
    filename is args.dumpPath.  File would be "args.dumpPath" with suffix .tar.bz2 or .zip
    """

    s3 = _get_aws_client(profile=profile, region=region, service="s3")

    if archive:
        if archive == "tar":
            archive_type = "tar.bz2"
        else:
            archive_type = "zip"

    # Make sure bucket exists before continuing
    try:
        s3.head_bucket(Bucket=bucket)
    except s3.exceptions.ClientError as e:
        logging.exception(
            "S3 bucket " + bucket + " does not exist. "
            "Can't get backup file\n\n" + str(e)
        )
        sys.exit(1)

    try:
        contents = s3.list_objects_v2(Bucket=bucket, Prefix=args.dumpPath)
    except s3.exceptions.ClientError as e:
        logging.exception(
            "Issue listing contents of bucket " + bucket + "\n\n" + str(e)
        )
        sys.exit(1)

    # Script will always overwrite older backup.  Bucket versioning stores multiple backups.
    # Therefore, just get item from bucket based on table name since that's what we name the files.
    filename = None
    for d in contents["Contents"]:
        if d["Key"] == "{}/{}.{}".format(args.dumpPath, table, archive_type):
            filename = d["Key"]

    if not filename:
        logging.exception(
            "Unable to find file to restore from.  "
            "Confirm the name of the table you're restoring."
        )
        sys.exit(1)

    output_file = "/tmp/" + os.path.basename(filename)
    logging.info("Downloading file " + filename + " to " + output_file)
    s3.download_file(bucket, filename, output_file)

    # Extract archive based on suffix
    if tarfile.is_tarfile(output_file):
        try:
            logging.info("Extracting tar file...")
            with tarfile.open(name=output_file, mode="r:bz2") as a:
                a.extractall(path=".")
        except tarfile.ReadError as e:
            logging.exception("Error reading downloaded archive\n\n" + str(e))
            sys.exit(1)
        except tarfile.ExtractError as e:
            # ExtractError is raised for non-fatal errors on extract method
            logging.error("Error during extraction: " + str(e))

    # Assuming zip file here since we're only supporting tar and zip at this time
    else:
        try:
            logging.info("Extracting zip file...")
            with zipfile.ZipFile(output_file, "r") as z:
                z.extractall(path=".")
        except zipfile.BadZipFile as e:
            logging.exception("Problem extracting zip file\n\n" + str(e))
            sys.exit(1)


def do_archive(archive_type, dump_path):
    """
    Create compressed archive of dump_path.

    Accepts archive_type of zip or tar and requires dump_path, directory added to archive
    """

    archive_base = dump_path

    if archive_type.lower() == "tar":
        archive = archive_base + ".tar.bz2"
        try:
            logging.info("Creating tar file " + archive + "...")
            with tarfile.open(name=archive, mode="w:bz2") as a:
                for root, dirs, files in os.walk(archive_base):
                    for file in files:
                        a.add(os.path.join(root, file))
                return True, archive
        except tarfile.CompressionError as e:
            logging.exception(
                "compression method is not supported or the data cannot be"
                " decoded properly.\n\n" + str(e)
            )
            sys.exit(1)
        except tarfile.TarError as e:
            logging.exception("Error creating tarfile archive.\n\n" + str(e))
            sys.exit(1)

    elif archive_type.lower() == "zip":
        try:
            logging.info("Creating zip file...")
            archive = archive_base + ".zip"
            with zipfile.ZipFile(archive, "w") as z:
                for root, dirs, files in os.walk(archive_base):
                    for file in files:
                        z.write(os.path.join(root, file))
                return True, archive
        except zipfile.BadZipFile as e:
            logging.exception("Problem creating zip file\n\n" + str(e))
            sys.exit(1)
        except zipfile.LargeZipFile:
            logging.exception(
                "Zip file would be too large.  Update code to use Zip64 to continue."
            )
            sys.exit(1)

    else:
        logging.error(
            "Unsupported archive format received.  Probably shouldn't have "
            "made it to this code path.  Skipping attempt at creating archive file"
        )
        return False, None


def get_table_name_matches(conn, table_name_wildcard):
    """
    Find tables to backup
    """

    all_tables = []
    last_evaluated_table_name = None

    while True:
        optional_args = {}
        if last_evaluated_table_name is not None:
            optional_args["ExclusiveStartTableName"] = last_evaluated_table_name
        table_list = conn.list_tables(**optional_args)
        all_tables.extend(table_list["TableNames"])

        try:
            last_evaluated_table_name = table_list["LastEvaluatedTableName"]
        except KeyError:
            break

    matching_tables = []
    for table_name in all_tables:
        if fnmatch.fnmatch(table_name, table_name_wildcard):
            logging.info("Adding %s", table_name)
            matching_tables.append(table_name)

    return matching_tables


def get_restore_table_matches(table_name_wildcard, separator):
    """
    Find tables to restore
    """

    matching_tables = []
    try:
        dir_list = os.listdir("./" + args.dumpPath)
    except OSError:
        logging.info(
            'Cannot find "./%s", Now trying user provided absolute dump path..'
            % args.dumpPath
        )
        try:
            dir_list = os.listdir(args.dumpPath)
        except OSError:
            logging.info(
                'Cannot find "%s", Now trying current working directory..'
                % args.dumpPath
            )
            dump_data_path = CURRENT_WORKING_DIR
            try:
                dir_list = os.listdir(dump_data_path)
            except OSError:
                logging.info(
                    'Cannot find "%s" directory containing dump files!' % dump_data_path
                )
                sys.exit(1)

    for dir_name in dir_list:
        if table_name_wildcard == "*":
            matching_tables.append(dir_name)
        elif separator == "":
            if dir_name.startswith(
                re.sub(
                    r"([A-Z])", r" \1", table_name_wildcard.split("*", 1)[0]
                ).split()[0]
            ):
                matching_tables.append(dir_name)
        elif dir_name.split(separator, 1)[0] == table_name_wildcard.split("*", 1)[0]:
            matching_tables.append(dir_name)

    return matching_tables


def change_prefix(source_table_name, source_wildcard, destination_wildcard, separator):
    """
    Update prefix used for searching tables
    """

    source_prefix = source_wildcard.split("*", 1)[0]
    destination_prefix = destination_wildcard.split("*", 1)[0]
    if separator == "":
        if re.sub(r"([A-Z])", r" \1", source_table_name).split()[0] == source_prefix:
            return destination_prefix + re.sub(
                r"([A-Z])", r" \1", source_table_name
            ).split(" ", 1)[1].replace(" ", "")
    if source_table_name.split(separator, 1)[0] == source_prefix:
        return destination_prefix + separator + source_table_name.split(separator, 1)[1]


def delete_table(conn, sleep_interval: int, table_name: str):
    """
    Delete table table_name
    """

    if not args.dataOnly:
        if not args.noConfirm:
            confirmation = input(
                "About to delete table {}. Type 'yes' to continue: ".format(table_name)
            )
            if confirmation != "yes":
                logging.warn("Confirmation not received. Stopping.")
                sys.exit(1)
        while True:
            # delete table if exists
            table_exist = True
            try:
                conn.delete_table(TableName=table_name)
            except conn.exceptions.ResourceNotFoundException:
                table_exist = False
                logging.info(table_name + " table deleted!")
                break
            except conn.exceptions.LimitExceededException:
                logging.info(
                    "Limit exceeded, retrying deletion of " + table_name + ".."
                )
                time.sleep(sleep_interval)
            except conn.exceptions.ProvisionedThroughputExceededException:
                logging.info(
                    "Control plane limit exceeded, retrying deletion of "
                    + table_name
                    + ".."
                )
                time.sleep(sleep_interval)
            except conn.exceptions.ResourceInUseException:
                logging.info(table_name + " table is being deleted..")
                time.sleep(sleep_interval)
            except conn.exceptions.ClientError as e:
                logging.exception(e)
                sys.exit(1)

        # if table exists, wait till deleted
        if table_exist:
            try:
                while True:
                    logging.info(
                        "Waiting for "
                        + table_name
                        + " table to be deleted.. ["
                        + conn.describe_table(table_name)["Table"]["TableStatus"]
                        + "]"
                    )
                    time.sleep(sleep_interval)
            except conn.exceptions.ResourceNotFoundException:
                logging.info(table_name + " table deleted.")
            except conn.exceptions.ClientError as e:
                logging.exception(e)
                sys.exit(1)


def mkdir_p(path):
    """
    Create directory to hold dump
    """

    try:
        os.makedirs(path)
    except OSError as exc:
        if not (exc.errno == errno.EEXIST and os.path.isdir(path)):
            raise


def batch_write(conn, sleep_interval, table_name, put_requests):
    """
    Write data to table_name
    """

    request_items = {table_name: put_requests}
    i = 1
    sleep = sleep_interval
    while True:
        response = conn.batch_write_item(RequestItems=request_items)
        unprocessed_items = response["UnprocessedItems"]

        if len(unprocessed_items) == 0:
            break
        if len(unprocessed_items) > 0 and i <= MAX_RETRY:
            logging.debug(
                str(len(unprocessed_items))
                + " unprocessed items, retrying after %s seconds.. [%s/%s]"
                % (str(sleep), str(i), str(MAX_RETRY))
            )
            request_items = unprocessed_items
            time.sleep(sleep)
            sleep += sleep_interval
            i += 1
        else:
            logging.info(
                "Max retries reached, failed to processed batch write: "
                + json.dumps(unprocessed_items, indent=JSON_INDENT)
            )
            logging.info("Ignoring and continuing..")
            break


def wait_for_active_table(conn, table_name, verb):
    """
    Wait for table to be indesired state
    """

    while True:
        if (
            conn.describe_table(TableName=table_name)["Table"]["TableStatus"]
            != "ACTIVE"
        ):
            logging.info(
                "Waiting for "
                + table_name
                + " table to be "
                + verb
                + ".. ["
                + conn.describe_table(TableName=table_name)["Table"]["TableStatus"]
                + "]"
            )
            time.sleep(sleep_interval)
        else:
            logging.info(table_name + " " + verb + ".")
            break


def update_provisioned_throughput(
    conn, table_name, read_capacity, write_capacity, wait=True
):
    """
    Update provisioned throughput on the table to provided values
    """

    logging.info(
        "Updating "
        + table_name
        + " table read capacity to: "
        + str(read_capacity)
        + ", write capacity to: "
        + str(write_capacity)
    )
    while True:
        try:
            conn.update_table(
                TableName=table_name,
                ProvisionedThroughput={
                    "ReadCapacityUnits": int(read_capacity),
                    "WriteCapacityUnits": int(write_capacity),
                },
            )
            break
        except conn.exceptions.ResourceNotFoundException:
            logging.info(
                "Limit exceeded, retrying updating throughput of " + table_name + ".."
            )
            time.sleep(sleep_interval)
        except conn.exceptions.ProvisionedThroughputExceededException:
            logging.info(
                "Control plane limit exceeded, retrying updating throughput"
                "of " + table_name + ".."
            )
            time.sleep(sleep_interval)

    # wait for provisioned throughput update completion
    if wait:
        wait_for_active_table(conn, table_name, "updated")


def do_empty(dynamo, table_name, billing_mode):
    """
    Empty table named table_name
    """

    logging.info("Starting Empty for " + table_name + "..")

    # get table schema
    logging.info("Fetching table schema for " + table_name)
    table_data = dynamo.describe_table(TableName=table_name)

    table_desc = table_data["Table"]
    table_attribute_definitions = table_desc["AttributeDefinitions"]
    table_key_schema = table_desc["KeySchema"]
    original_read_capacity = table_desc["ProvisionedThroughput"]["ReadCapacityUnits"]
    original_write_capacity = table_desc["ProvisionedThroughput"]["WriteCapacityUnits"]
    table_local_secondary_indexes = table_desc.get("LocalSecondaryIndexes")
    table_global_secondary_indexes = table_desc.get("GlobalSecondaryIndexes")

    optional_args = {}
    if billing_mode == PROVISIONED_BILLING_MODE:
        table_provisioned_throughput = {
            "ReadCapacityUnits": int(original_read_capacity),
            "WriteCapacityUnits": int(original_write_capacity),
        }
        optional_args["ProvisionedThroughput"] = table_provisioned_throughput

    if table_local_secondary_indexes is not None:
        optional_args["LocalSecondaryIndexes"] = table_local_secondary_indexes

    if table_global_secondary_indexes is not None:
        optional_args["GlobalSecondaryIndexes"] = table_global_secondary_indexes

    table_provisioned_throughput = {
        "ReadCapacityUnits": int(original_read_capacity),
        "WriteCapacityUnits": int(original_write_capacity),
    }

    logging.info("Deleting Table " + table_name)

    delete_table(dynamo, sleep_interval, table_name)

    logging.info("Creating Table " + table_name)

    while True:
        try:
            dynamo.create_table(
                AttributeDefinitions=table_attribute_definitions,
                TableName=table_name,
                KeySchema=table_key_schema,
                BillingMode=billing_mode,
                **optional_args
            )
            break
        except dynamo.exceptions.LimitExceededException:
            logging.info("Limit exceeded, retrying creation of " + table_name + "..")
            time.sleep(sleep_interval)
        except dynamo.exceptions.ProvisionedThroughputExceededException:
            logging.info(
                "Control plane limit exceeded, retrying creation of "
                + table_name
                + ".."
            )
            time.sleep(sleep_interval)
        except dynamo.exceptions.ClientError as e:
            logging.exception(e)
            sys.exit(1)

    # wait for table creation completion
    wait_for_active_table(dynamo, table_name, "created")

    logging.info(
        "Recreation of "
        + table_name
        + " completed. Time taken: "
        + str(datetime.datetime.now().replace(microsecond=0) - start_time)
    )


def do_backup(
    dynamo,
    read_capacity,
    table_queue=None,
    src_table=None,
    filter_option=None,
    limit=None,
):
    """
    Connect to DynamoDB and perform the backup for src_table or each table in table_queue
    """

    if src_table:
        table_name = src_table

    if table_queue:
        while True:
            table_name = table_queue.get()
            if table_name is None:
                break

            logging.info("Starting backup for " + table_name + "..")

            # trash data, re-create subdir
            if os.path.exists(args.dumpPath + os.sep + table_name):
                shutil.rmtree(args.dumpPath + os.sep + table_name)
            mkdir_p(args.dumpPath + os.sep + table_name)

            # get table schema
            logging.info("Dumping table schema for " + table_name)
            f = open(args.dumpPath + os.sep + table_name + os.sep + SCHEMA_FILE, "w+")
            table_desc = dynamo.describe_table(TableName=table_name)
            f.write(json.dumps(table_desc, indent=JSON_INDENT))
            f.close()

            if not args.schemaOnly:
                original_read_capacity = table_desc["Table"]["ProvisionedThroughput"][
                    "ReadCapacityUnits"
                ]
                original_write_capacity = table_desc["Table"]["ProvisionedThroughput"][
                    "WriteCapacityUnits"
                ]

                # override table read capacity if specified
                if (
                    read_capacity is not None
                    and read_capacity != original_read_capacity
                ):
                    update_provisioned_throughput(
                        dynamo, table_name, read_capacity, original_write_capacity
                    )

                # get table data
                logging.info("Dumping table items for " + table_name)
                mkdir_p(args.dumpPath + os.sep + table_name + os.sep + DATA_DIR)

                i = 1
                num_items = 0
                last_evaluated_key = None

                while True:
                    try:
                        optional_args = {}
                        if last_evaluated_key is not None:
                            optional_args["ExclusiveStartKey"] = last_evaluated_key
                        if filter_option is not None:
                            optional_args.update(filter_option)
                        scanned_table = dynamo.scan(
                            TableName=table_name, **optional_args
                        )
                    except dynamo.exceptions.ProvisionedThroughputExceededException:
                        logging.error(
                            "EXCEEDED THROUGHPUT ON TABLE "
                            + table_name
                            + ".  BACKUP FOR IT IS USELESS."
                        )
                        table_queue.task_done()

                    f = open(
                        args.dumpPath
                        + os.sep
                        + table_name
                        + os.sep
                        + DATA_DIR
                        + os.sep
                        + str(i).zfill(4)
                        + ".json",
                        "w+",
                    )
                    del scanned_table["ResponseMetadata"]

                    f.write(json.dumps(scanned_table, indent=JSON_INDENT))
                    f.close()

                    i += 1

                    num_items += len(scanned_table["Items"])
                    if limit and num_items > limit:
                        break

                    try:
                        last_evaluated_key = scanned_table["LastEvaluatedKey"]
                    except KeyError:
                        break

                # revert back to original table read capacity if specified
                if (
                    read_capacity is not None
                    and read_capacity != original_read_capacity
                ):
                    update_provisioned_throughput(
                        dynamo,
                        table_name,
                        original_read_capacity,
                        original_write_capacity,
                        False,
                    )

                logging.info(
                    "Backup for "
                    + table_name
                    + " table completed. Time taken: "
                    + str(datetime.datetime.now().replace(microsecond=0) - start_time)
                )

            table_queue.task_done()


def prepare_provisioned_throughput_for_restore(provisioned_throughput):
    """
    This function makes sure that the payload returned for the boto3 API call create_table is compatible
    with the provisioned throughput attribute
    See: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb.html
    """
    return {
        "ReadCapacityUnits": provisioned_throughput["ReadCapacityUnits"],
        "WriteCapacityUnits": provisioned_throughput["WriteCapacityUnits"],
    }


def prepare_lsi_for_restore(lsi):
    """
    This function makes sure that the payload returned for the boto3 API call create_table is compatible
    See: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb.html#DynamoDB.Client.create_table
    """
    return {
        "IndexName": lsi["IndexName"],
        "KeySchema": lsi["KeySchema"],
        "Projection": lsi["Projection"],
    }


def prepare_gsi_for_restore(gsi, billing_mode):
    """
    This function makes sure that the payload returned for the boto3 API call create_table is compatible
    See: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb.html
    """
    result = {
        "IndexName": gsi["IndexName"],
        "KeySchema": gsi["KeySchema"],
        "Projection": gsi["Projection"],
    }

    if billing_mode != PAY_PER_REQUEST_BILLING_MODE:
        result["ProvisionedThroughput"] = prepare_provisioned_throughput_for_restore(
            gsi["ProvisionedThroughput"]
        )

    return result


def do_restore(
    dynamo,
    sleep_interval,
    source_table,
    destination_table,
    write_capacity,
    billing_mode,
):
    """
    Restore table
    """
    logging.info(
        "Starting restore for " + source_table + " to " + destination_table + ".."
    )

    # create table using schema
    # restore source_table from dump directory if it exists else try current working directory
    if os.path.exists("%s/%s" % (args.dumpPath, source_table)):
        dump_data_path = args.dumpPath
    else:
        logging.info(
            'Cannot find "./%s/%s", Now trying current working directory..'
            % (args.dumpPath, source_table)
        )
        if os.path.exists("%s/%s" % (CURRENT_WORKING_DIR, source_table)):
            dump_data_path = CURRENT_WORKING_DIR
        else:
            logging.info(
                'Cannot find "%s/%s" directory containing dump files!'
                % (CURRENT_WORKING_DIR, source_table)
            )
            sys.exit(1)
    table_data = json.load(
        open(dump_data_path + os.sep + source_table + os.sep + SCHEMA_FILE)
    )
    table = table_data["Table"]
    table_attribute_definitions = table["AttributeDefinitions"]
    table_table_name = destination_table
    table_key_schema = table["KeySchema"]
    original_read_capacity = table["ProvisionedThroughput"]["ReadCapacityUnits"]
    original_write_capacity = table["ProvisionedThroughput"]["WriteCapacityUnits"]
    table_local_secondary_indexes = table.get("LocalSecondaryIndexes")
    table_global_secondary_indexes = table.get("GlobalSecondaryIndexes")

    # override table write capacity if specified, else use RESTORE_WRITE_CAPACITY if original
    # write capacity is lower
    if write_capacity is None:
        if original_write_capacity < RESTORE_WRITE_CAPACITY:
            write_capacity = RESTORE_WRITE_CAPACITY
        else:
            write_capacity = original_write_capacity

    if original_write_capacity == 0:
        original_write_capacity = RESTORE_WRITE_CAPACITY

    # ensure that read capacity is at least RESTORE_READ_CAPACITY
    if original_read_capacity < RESTORE_READ_CAPACITY:
        read_capacity = RESTORE_WRITE_CAPACITY
    else:
        read_capacity = original_read_capacity

    if original_read_capacity == 0:
        original_read_capacity = RESTORE_READ_CAPACITY

    # override GSI write capacities if specified, else use RESTORE_WRITE_CAPACITY if original
    # write capacity is lower
    original_gsi_write_capacities = []
    original_gsi_read_capacities = []
    if table_global_secondary_indexes is not None:
        for gsi in table_global_secondary_indexes:
            # keeps track of original gsi write capacity units. If provisioned capacity is 0, set to
            # RESTORE_WRITE_CAPACITY as fallback given that 0 is not allowed for write capacities
            original_gsi_write_capacity = gsi["ProvisionedThroughput"][
                "WriteCapacityUnits"
            ]
            if original_gsi_write_capacity == 0:
                original_gsi_write_capacity = RESTORE_WRITE_CAPACITY

            original_gsi_write_capacities.append(original_gsi_write_capacity)

            if gsi["ProvisionedThroughput"]["WriteCapacityUnits"] < int(write_capacity):
                gsi["ProvisionedThroughput"]["WriteCapacityUnits"] = int(write_capacity)

            # keeps track of original gsi read capacity units. If provisioned capacity is 0, set to
            # RESTORE_READ_CAPACITY as fallback given that 0 is not allowed for read capacities
            original_gsi_read_capacity = gsi["ProvisionedThroughput"][
                "ReadCapacityUnits"
            ]
            if original_gsi_read_capacity == 0:
                original_gsi_read_capacity = RESTORE_READ_CAPACITY

            original_gsi_read_capacities.append(original_gsi_read_capacity)

            if (
                gsi["ProvisionedThroughput"]["ReadCapacityUnits"]
                < RESTORE_READ_CAPACITY
            ):
                gsi["ProvisionedThroughput"][
                    "ReadCapacityUnits"
                ] = RESTORE_READ_CAPACITY

    # temp provisioned throughput for restore
    table_provisioned_throughput = {
        "ReadCapacityUnits": int(read_capacity),
        "WriteCapacityUnits": int(write_capacity),
    }

    optional_args = {}
    if billing_mode == PROVISIONED_BILLING_MODE:
        optional_args["ProvisionedThroughput"] = table_provisioned_throughput

    if not args.dataOnly:
        logging.info(
            "Creating "
            + destination_table
            + " table with temp write capacity of "
            + str(write_capacity)
        )

        if table_local_secondary_indexes is not None:
            optional_args["LocalSecondaryIndexes"] = [
                prepare_lsi_for_restore(gsi) for gsi in table_local_secondary_indexes
            ]

        if table_global_secondary_indexes is not None:
            optional_args["GlobalSecondaryIndexes"] = [
                prepare_gsi_for_restore(gsi, billing_mode)
                for gsi in table_global_secondary_indexes
            ]

        while True:
            try:
                dynamo.create_table(
                    AttributeDefinitions=table_attribute_definitions,
                    TableName=table_table_name,
                    KeySchema=table_key_schema,
                    BillingMode=billing_mode,
                    **optional_args
                )
                break
            except dynamo.exceptions.LimitExceededException:
                logging.info(
                    "Limit exceeded, retrying creation of " + destination_table + ".."
                )
                time.sleep(sleep_interval)
            except dynamo.exceptions.ProvisionedThroughputExceededException:
                logging.info(
                    "Control plane limit exceeded, "
                    "retrying creation of " + destination_table + ".."
                )
                time.sleep(sleep_interval)
            except dynamo.exceptions.ClientError as e:
                logging.exception(e)
                sys.exit(1)

        # wait for table creation completion
        wait_for_active_table(dynamo, destination_table, "created")
    elif not args.skipThroughputUpdate:
        # update provisioned capacity
        if int(write_capacity) > original_write_capacity:
            update_provisioned_throughput(
                dynamo, destination_table, original_read_capacity, write_capacity, False
            )

    if not args.schemaOnly:
        # read data files
        logging.info("Restoring data for " + destination_table + " table..")
        data_file_list = os.listdir(
            dump_data_path + os.sep + source_table + os.sep + DATA_DIR + os.sep
        )
        data_file_list.sort()

        for data_file in data_file_list:
            logging.info("Processing " + data_file + " of " + destination_table)
            items = []
            item_data = json.load(
                open(
                    dump_data_path
                    + os.sep
                    + source_table
                    + os.sep
                    + DATA_DIR
                    + os.sep
                    + data_file
                ),
            )
            process_item_types(item_data)
            items.extend(item_data["Items"])

            # batch write data
            put_requests = []
            while len(items) > 0:
                put_requests.append({"PutRequest": {"Item": items.pop(0)}})

                # flush every MAX_BATCH_WRITE
                if len(put_requests) == MAX_BATCH_WRITE:
                    logging.debug(
                        "Writing next "
                        + str(MAX_BATCH_WRITE)
                        + " items to "
                        + destination_table
                        + ".."
                    )
                    batch_write(
                        dynamo,
                        BATCH_WRITE_SLEEP_INTERVAL,
                        destination_table,
                        put_requests,
                    )
                    del put_requests[:]

            # flush remainder
            if len(put_requests) > 0:
                batch_write(
                    dynamo, BATCH_WRITE_SLEEP_INTERVAL, destination_table, put_requests
                )

        if not args.skipThroughputUpdate:
            # revert to original table write capacity if it has been modified
            if (
                int(write_capacity) != original_write_capacity
                or int(read_capacity) != original_read_capacity
            ):
                update_provisioned_throughput(
                    dynamo,
                    destination_table,
                    original_read_capacity,
                    original_write_capacity,
                    False,
                )

            # loop through each GSI to check if it has changed and update if necessary
            if table_global_secondary_indexes is not None:
                gsi_data = []
                for gsi in table_global_secondary_indexes:
                    wcu = gsi["ProvisionedThroughput"]["WriteCapacityUnits"]
                    rcu = gsi["ProvisionedThroughput"]["ReadCapacityUnits"]
                    original_gsi_write_capacity = original_gsi_write_capacities.pop(0)
                    original_gsi_read_capacity = original_gsi_read_capacities.pop(0)
                    if (
                        original_gsi_write_capacity != wcu
                        or original_gsi_read_capacity != rcu
                    ):
                        gsi_data.append(
                            {
                                "Update": {
                                    "IndexName": gsi["IndexName"],
                                    "ProvisionedThroughput": {
                                        "ReadCapacityUnits": int(
                                            original_gsi_read_capacity
                                        ),
                                        "WriteCapacityUnits": int(
                                            original_gsi_write_capacity
                                        ),
                                    },
                                }
                            }
                        )

                if gsi_data:
                    logging.info(
                        "Updating "
                        + destination_table
                        + " global secondary indexes write and read capacities as necessary.."
                    )
                    while True:
                        try:
                            dynamo.update_table(
                                TableName=destination_table,
                                GlobalSecondaryIndexUpdates=gsi_data,
                            )
                            break
                        except dynamo.exceptions.LimitExceededException:
                            logging.info(
                                "Limit exceeded, retrying updating throughput of"
                                "GlobalSecondaryIndexes in " + destination_table + ".."
                            )
                            time.sleep(sleep_interval)
                        except dynamo.exceptions.ProvisionedThroughputExceededException:
                            logging.info(
                                "Control plane limit exceeded, retrying updating throughput of"
                                "GlobalSecondaryIndexes in " + destination_table + ".."
                            )
                            time.sleep(sleep_interval)

        # wait for table to become active
        wait_for_active_table(dynamo, destination_table, "active")

        logging.info(
            "Restore for "
            + source_table
            + " to "
            + destination_table
            + " table completed. Time taken: "
            + str(datetime.datetime.now().replace(microsecond=0) - start_time)
        )
    else:
        logging.info(
            "Empty schema of "
            + source_table
            + " table created. Time taken: "
            + str(datetime.datetime.now().replace(microsecond=0) - start_time)
        )


def main():
    """
    Entrypoint to the script
    """

    global args, sleep_interval, start_time

    # parse args
    parser = argparse.ArgumentParser(
        description="Simple DynamoDB backup/restore/empty."
    )
    parser.add_argument(
        "-a",
        "--archive",
        help="Type of compressed archive to create. If unset, don't create archive",
        choices=["zip", "tar"],
    )
    parser.add_argument(
        "-b",
        "--bucket",
        help="S3 bucket in which to store or retrieve backups. [must already exist]",
    )
    parser.add_argument(
        "-m",
        "--mode",
        help="Operation to perform",
        choices=["backup", "restore", "empty"],
    )
    parser.add_argument(
        "-r",
        "--region",
        help="AWS region to use, e.g. 'us-west-1'. "
        "Can use any region for local testing",
    )
    parser.add_argument(
        "--host",
        help="Host of local DynamoDB. This parameter initialises dynamodump for local DynamoDB testing [required only for local]",
    )
    parser.add_argument(
        "--port", help="Port of local DynamoDB [required only for local]"
    )
    parser.add_argument(
        "--accessKey", help="Access key of local DynamoDB [required only for local]"
    )
    parser.add_argument(
        "--secretKey", help="Secret key of local DynamoDB [required only for local]"
    )
    parser.add_argument(
        "-p",
        "--profile",
        help="AWS credentials file profile to use. Allows you to use a "
        "profile instead accessKey, secretKey authentication",
    )
    parser.add_argument(
        "-s",
        "--srcTable",
        help="Source DynamoDB table name to backup or restore from, "
        "use 'tablename*' for wildcard prefix selection or '*' for "
        "all tables.  Mutually exclusive with --tag",
    )
    parser.add_argument(
        "-d",
        "--destTable",
        help="Destination DynamoDB table name to backup or restore to, "
        "use 'tablename*' for wildcard prefix selection "
        "(defaults to use '-' separator) [optional, defaults to source]",
    )
    parser.add_argument(
        "--prefixSeparator",
        help="Specify a different prefix separator, e.g. '.' [optional]",
    )
    parser.add_argument(
        "--noSeparator",
        action="store_true",
        help="Overrides the use of a prefix separator for backup wildcard "
        "searches [optional]",
    )
    parser.add_argument(
        "--readCapacity",
        help="Change the temp read capacity of the DynamoDB table to backup "
        "from [optional]",
    )
    parser.add_argument(
        "-t",
        "--tag",
        help="Tag to use for identifying tables to back up.  "
        "Mutually exclusive with srcTable.  Provided as KEY=VALUE",
    )
    parser.add_argument(
        "--writeCapacity",
        help="Change the temp write capacity of the DynamoDB table to restore "
        "to [defaults to " + str(RESTORE_WRITE_CAPACITY) + ", optional]",
    )
    parser.add_argument(
        "--schemaOnly",
        action="store_true",
        default=False,
        help="Backup or restore the schema only. Do not backup/restore data. "
        "Can be used with both backup and restore modes. Cannot be used with "
        "the --dataOnly [optional]",
    )
    parser.add_argument(
        "--dataOnly",
        action="store_true",
        default=False,
        help="Restore data only. Do not delete/recreate schema [optional for "
        "restore]",
    )
    parser.add_argument(
        "--noConfirm",
        action="store_true",
        default=False,
        help="Don't ask for confirmation before deleting existing schemas.",
    )
    parser.add_argument(
        "--skipThroughputUpdate",
        action="store_true",
        default=False,
        help="Skip updating throughput values across tables [optional]",
    )
    parser.add_argument(
        "--dumpPath",
        help="Directory to place and search for DynamoDB table "
        "backups (defaults to use '" + str(DATA_DUMP) + "') [optional]",
        default=str(DATA_DUMP),
    )
    parser.add_argument(
        "--billingMode",
        help="Set billing mode between "
        + str(PROVISIONED_BILLING_MODE)
        + "|"
        + str(PAY_PER_REQUEST_BILLING_MODE)
        + " (defaults to use '"
        + str(PROVISIONED_BILLING_MODE)
        + "') [optional]",
        choices=[PROVISIONED_BILLING_MODE, PAY_PER_REQUEST_BILLING_MODE],
        default=str(PROVISIONED_BILLING_MODE),
    )
    parser.add_argument(
        "--log", help="Logging level - DEBUG|INFO|WARNING|ERROR|CRITICAL [optional]"
    )
    parser.add_argument(
        "--limit",
        help="Limit option for backup, will stop the back up process after number of backed up items reaches the limit [optional]",
        type=int,
    )
    parser.add_argument(
        "-f",
        "--filterOption",
        help="Filter option for backup, JSON file of which keys are ['FilterExpression', 'ExpressionAttributeNames', 'ExpressionAttributeValues']",
    )
    args = parser.parse_args()

    # set log level
    log_level = LOG_LEVEL
    if args.log is not None:
        log_level = args.log.upper()
    logging.basicConfig(level=getattr(logging, log_level))

    # Check to make sure that --dataOnly and --schemaOnly weren't simultaneously specified
    if args.schemaOnly and args.dataOnly:
        logging.info("Options --schemaOnly and --dataOnly are mutually exclusive.")
        sys.exit(1)

    # instantiate connection
    if args.host:
        conn = _get_aws_client(
            service="dynamodb",
            access_key=args.accessKey,
            secret_key=args.secretKey,
            region=args.region,
            endpoint_url="http://" + args.host + ":" + args.port,
        )
        sleep_interval = LOCAL_SLEEP_INTERVAL
    else:
        if not args.profile:
            conn = _get_aws_client(
                service="dynamodb",
                access_key=args.accessKey,
                secret_key=args.secretKey,
                region=args.region,
            )
            sleep_interval = AWS_SLEEP_INTERVAL
        else:
            conn = _get_aws_client(
                service="dynamodb",
                profile=args.profile,
                region=args.region,
            )
            sleep_interval = AWS_SLEEP_INTERVAL

    # don't proceed if connection is not established
    if not conn:
        logging.info("Unable to establish connection with dynamodb")
        sys.exit(1)

    # set prefix separator
    prefix_separator = DEFAULT_PREFIX_SEPARATOR
    if args.prefixSeparator is not None:
        prefix_separator = args.prefixSeparator
    if args.noSeparator is True:
        prefix_separator = None

    # set filter options
    filter_option = None
    if args.filterOption is not None:
        with open(args.filterOption, "r") as f:
            filter_option = json.load(f)
            if filter_option.keys() != set(
                (
                    "FilterExpression",
                    "ExpressionAttributeNames",
                    "ExpressionAttributeValues",
                )
            ):
                raise Exception("Invalid filter option format")

    # do backup/restore
    start_time = datetime.datetime.now().replace(microsecond=0)
    if args.mode == "backup":
        matching_backup_tables = []
        if args.tag:
            # Use Boto3 to find tags.  Boto3 provides a paginator that makes searching ta
            matching_backup_tables = get_table_name_by_tag(
                args.profile, args.region, args.tag
            )
        elif args.srcTable.find("*") != -1:
            matching_backup_tables = get_table_name_matches(conn, args.srcTable)
        elif args.srcTable:
            matching_backup_tables.append(args.srcTable)

        if len(matching_backup_tables) == 0:
            logging.info("No matching tables found.  Nothing to do.")
            sys.exit(0)
        else:
            logging.info(
                "Found "
                + str(len(matching_backup_tables))
                + " table(s) in DynamoDB host to backup: "
                + ", ".join(matching_backup_tables)
            )

        try:
            if args.srcTable.find("*") == -1:
                do_backup(
                    conn,
                    args.read_capacity,
                    table_queue=None,
                    filter_option=filter_option,
                    limit=args.limit,
                )
            else:
                do_backup(
                    conn,
                    args.read_capacity,
                    matching_backup_tables,
                    filter_option=filter_option,
                    limit=args.limit,
                )
        except AttributeError:
            # Didn't specify srcTable if we get here

            q = Queue()
            threads = []

            for _ in range(MAX_NUMBER_BACKUP_WORKERS):
                t = threading.Thread(
                    target=do_backup,
                    args=(conn, args.readCapacity),
                    kwargs={
                        "table_queue": q,
                        "filter_option": filter_option,
                        "limit": args.limit,
                    },
                )
                t.start()
                threads.append(t)
                time.sleep(THREAD_START_DELAY)

            for table in matching_backup_tables:
                q.put(table)

            q.join()

            for _ in range(MAX_NUMBER_BACKUP_WORKERS):
                q.put(None)
            for t in threads:
                t.join()

            try:
                logging.info("Backup of table(s) " + args.srcTable + " completed!")
            except (NameError, TypeError):
                logging.info(
                    "Backup of table(s) "
                    + ", ".join(matching_backup_tables)
                    + " completed!"
                )

            if args.archive:
                if args.tag:
                    for table in matching_backup_tables:
                        dump_path = args.dumpPath + os.sep + table
                        did_archive, archive_file = do_archive(args.archive, dump_path)
                        if args.bucket and did_archive:
                            do_put_bucket_object(
                                args.profile, args.region, args.bucket, archive_file
                            )
                else:
                    did_archive, archive_file = do_archive(args.archive, args.dumpPath)

                if args.bucket and did_archive:
                    do_put_bucket_object(
                        args.profile, args.region, args.bucket, archive_file
                    )

    elif args.mode == "restore":
        if args.destTable is not None:
            dest_table = args.destTable
        else:
            dest_table = args.srcTable

        # If backups are in S3 download and extract the backup to use during restoration
        if args.bucket:
            do_get_s3_archive(
                args.profile, args.region, args.bucket, args.srcTable, args.archive
            )

        if dest_table.find("*") != -1:
            matching_destination_tables = get_table_name_matches(conn, dest_table)
            delete_str = ": " if args.dataOnly else " to be deleted: "
            logging.info(
                "Found "
                + str(len(matching_destination_tables))
                + " table(s) in DynamoDB host"
                + delete_str
                + ", ".join(matching_destination_tables)
            )

            threads = []
            for table in matching_destination_tables:
                t = threading.Thread(
                    target=delete_table, args=(conn, sleep_interval, table)
                )
                threads.append(t)
                t.start()
                time.sleep(THREAD_START_DELAY)

            for thread in threads:
                thread.join()

            matching_restore_tables = get_restore_table_matches(
                args.srcTable, prefix_separator
            )
            logging.info(
                "Found "
                + str(len(matching_restore_tables))
                + " table(s) in "
                + args.dumpPath
                + " to restore: "
                + ", ".join(matching_restore_tables)
            )

            threads = []
            for source_table in matching_restore_tables:
                if args.srcTable == "*":
                    t = threading.Thread(
                        target=do_restore,
                        args=(
                            conn,
                            sleep_interval,
                            source_table,
                            source_table,
                            args.writeCapacity,
                            args.billingMode,
                        ),
                    )
                else:
                    t = threading.Thread(
                        target=do_restore,
                        args=(
                            conn,
                            sleep_interval,
                            source_table,
                            change_prefix(
                                source_table,
                                args.srcTable,
                                dest_table,
                                prefix_separator,
                            ),
                            args.writeCapacity,
                            args.billingMode,
                        ),
                    )
                threads.append(t)
                t.start()
                time.sleep(THREAD_START_DELAY)

            for thread in threads:
                thread.join()

            logging.info(
                "Restore of table(s) "
                + args.srcTable
                + " to "
                + dest_table
                + " completed!"
            )
        else:
            delete_table(
                conn=conn, sleep_interval=sleep_interval, table_name=dest_table
            )
            do_restore(
                dynamo=conn,
                sleep_interval=sleep_interval,
                source_table=args.srcTable,
                destination_table=dest_table,
                write_capacity=args.writeCapacity,
                billing_mode=args.billingMode,
            )
    elif args.mode == "empty":
        if args.srcTable.find("*") != -1:
            matching_backup_tables = get_table_name_matches(conn, args.srcTable)
            logging.info(
                "Found "
                + str(len(matching_backup_tables))
                + " table(s) in DynamoDB host to empty: "
                + ", ".join(matching_backup_tables)
            )

            threads = []
            for table in matching_backup_tables:
                t = threading.Thread(
                    target=do_empty, args=(conn, table, args.billingMode)
                )
                threads.append(t)
                t.start()
                time.sleep(THREAD_START_DELAY)

            for thread in threads:
                thread.join()

            logging.info("Empty of table(s) " + args.srcTable + " completed!")
        else:
            do_empty(conn, args.srcTable, args.billingMode)


if __name__ == "__main__":
    main()
