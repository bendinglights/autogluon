#!/usr/bin/env bash

# This script build the docs and store the results into a intermediate bucket to prevent our web hosting bucket being manipulated intentionally
# The final docs will be copied to the web hosting bucket diromg GitHub workflow that runs in the context of the base repository's default branch

BRANCH=$(basename $1)
GIT_REPO=$2
COMMIT_SHA=$3
PR_NUMBER=$4

set -ex

source $(dirname "$0")/env_setup.sh

if [[ -n $PR_NUMBER ]]; then build_docs_path=build_docs/$PR_NUMBER/$COMMIT_SHA; else build_docs_path=build_docs/$BRANCH/$COMMIT_SHA; fi

if [[ (-n $PR_NUMBER) || ($GIT_REPO != awslabs/autogluon) ]]
then
    bucket='autogluon-doc-staging'
    if [[ -n $PR_NUMBER ]]; then path=$PR_NUMBER; else path=$BRANCH; fi
    site=$bucket.s3.amazonaws.com/$path/$COMMIT_SHA  # site is the actual bucket location that will serve the doc
else
    if [[ $BRANCH == 'master' ]]
    then
        path='dev'
    else
        if [[ $BRANCH == 'dev' ]]
        then
            path='dev-branch'
        else
            path=$BRANCH
        fi
    fi
    bucket='autogluon-website'
    site=$bucket/$path
fi

other_doc_version_text='Stable Version Documentation'
other_doc_version_branch='stable'
if [[ $BRANCH == 'stable' ]]
then
    other_doc_version_text='Dev Version Documentation'
    other_doc_version_branch='dev'
fi

mkdir -p docs/_build/rst/tutorials/
aws s3 cp s3://autogluon-ci/$build_docs_path docs/_build/rst/tutorials/ --recursive

setup_build_contrib_env
install_all
setup_mxnet_gpu
# setup_torch

sed -i -e "s@###_PLACEHOLDER_WEB_CONTENT_ROOT_###@http://$site@g" docs/config.ini
sed -i -e "s@###_OTHER_VERSIONS_DOCUMENTATION_LABEL_###@$other_doc_version_text@g" docs/config.ini
sed -i -e "s@###_OTHER_VERSIONS_DOCUMENTATION_BRANCH_###@$other_doc_version_branch@g" docs/config.ini

shopt -s extglob
rm -rf ./docs/tutorials/!(index.rst)
cd docs && d2lbook build rst && d2lbook build html

COMMAND_EXIT_CODE=$?
if [ $COMMAND_EXIT_CODE -ne 0 ]; then
    exit COMMAND_EXIT_CODE
fi

# Verify we still own the bucket
bucket_query=$(aws s3 ls | grep -E "(^| )autogluon-ci( |$)")
if [ ! -z bucket_query ]; then
    aws s3 cp --recursive _build/html/ s3://autogluon-ci/build_docs/${path}/$COMMIT_SHA/all --quiet
    echo "Uploaded doc to s3://autogluon-ci/build_docs/${path}/$COMMIT_SHA/all"
else
    echo Bucket does not belong to us anymore. Will not write to it
fi;

# Verify we still own the bucket
bucket_query=$(aws s3 ls | grep -E "(^| )autogluon-ci( |$)")
if [ ! -z bucket_query ]; then
    if [[ ($BRANCH == 'master') && ($REPO == awslabs/autogluon) ]]
    then
        aws s3 cp root_index.html s3://autogluon-ci/build_docs/${path}/$COMMIT_SHA/root_index.html
    fi
else
    echo Bucket does not belong to us anymore. Will not write to it
fi;
