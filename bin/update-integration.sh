#!/usr/bin/env bash

set -e

cd unisubs-integration
git fetch origin
git checkout `cat ../optional/unisubs-integration`

cd ../amara-enterprise
git fetch origin
git checkout `cat ../optional/amara-enterprise`
