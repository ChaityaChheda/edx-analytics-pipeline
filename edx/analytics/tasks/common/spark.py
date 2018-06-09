import ast
import json
import logging
import os
import shutil
import tempfile
import zipfile

import luigi
import luigi.configuration
from luigi.contrib.spark import PySparkTask

from edx.analytics.tasks.common.pathutil import EventLogSelectionDownstreamMixin, PathSelectionByDateIntervalTask
from edx.analytics.tasks.util.manifest import (
    ManifestInputTargetMixin, convert_to_manifest_input_if_necessary, remove_manifest_target_if_exists
)
from edx.analytics.tasks.util.overwrite import OverwriteOutputMixin
from edx.analytics.tasks.util.url import get_target_from_url, url_path_join

_file_path_to_package_meta_path = {}

log = logging.getLogger(__name__)


def get_package_metadata_paths():
    """
    List of package metadata to be loaded on EMR cluster
    """
    from distlib.database import DistributionPath

    if len(_file_path_to_package_meta_path) > 0:
        return _file_path_to_package_meta_path

    dist_path = DistributionPath(include_egg=True)
    for distribution in dist_path.get_distributions():
        metadata_path = distribution.path
        for installed_file_path, _hash, _size in distribution.list_installed_files():
            absolute_installed_file_path = installed_file_path
            if not os.path.isabs(installed_file_path):
                absolute_installed_file_path = os.path.join(os.path.dirname(metadata_path), installed_file_path)
            normalized_file_path = os.path.realpath(absolute_installed_file_path)
            _file_path_to_package_meta_path[normalized_file_path] = metadata_path

    return _file_path_to_package_meta_path


def dereference(f):
    if os.path.islink(f):
        # by joining with the dirname we are certain to get the absolute path
        return dereference(os.path.join(os.path.dirname(f), os.readlink(f)))
    else:
        return f


def create_packages_archive(packages, archive_dir_path):
    """
    Create a zip archive for all the packages listed in packages and returns the list of zip file location.
    """
    import zipfile
    archives_list = []
    package_metadata_paths = get_package_metadata_paths()
    metadata_to_add = dict()

    package_zip_path = os.path.join(archive_dir_path, 'packages.zip')
    package_zip = zipfile.ZipFile(package_zip_path, "w", compression=zipfile.ZIP_DEFLATED)
    archives_list.append(package_zip_path)

    def add(src, dst, package_name):
        # Ensure any entry points and other egg-info metadata is also transmitted along with
        # this file. If it is associated with any egg-info directories, ship them too.
        metadata_path = package_metadata_paths.get(os.path.realpath(src))
        if metadata_path:
            metadata_to_add[package_name] = metadata_path

        package_zip.write(src, dst)

    def add_files_for_package(sub_package_path, root_package_path, root_package_name, package_name):
        for root, dirs, files in os.walk(sub_package_path):
            if '.svn' in dirs:
                dirs.remove('.svn')
            for f in files:
                if not f.endswith(".pyc") and not f.startswith("."):
                    add(dereference(root + "/" + f),
                        root.replace(root_package_path, root_package_name) + "/" + f,
                        package_name)

    for package in packages:
        # Archive each package
        if not getattr(package, "__path__", None) and '.' in package.__name__:
            package = __import__(package.__name__.rpartition('.')[0], None, None, 'non_empty')

        n = package.__name__.replace(".", "/")

        # Check length of path, because the attribute may exist and be an empty list.
        if len(getattr(package, "__path__", [])) > 0:
            # TODO: (BUG) picking only the first path does not
            # properly deal with namespaced packages in different
            # directories
            p = package.__path__[0]

            if p.endswith('.egg') and os.path.isfile(p):
                raise 'Not going to archive egg files!!!'
                # Add the entire egg file
                # p = p[:p.find('.egg') + 4]
                # add(dereference(p), os.path.basename(p))

            else:
                # include __init__ files from parent projects
                root = []
                for parent in package.__name__.split('.')[0:-1]:
                    root.append(parent)
                    module_name = '.'.join(root)
                    directory = '/'.join(root)

                    add(dereference(__import__(module_name, None, None, 'non_empty').__path__[0] + "/__init__.py"),
                        directory + "/__init__.py",
                        package.__name__)

                add_files_for_package(p, p, n, package.__name__)

        else:
            f = package.__file__
            if f.endswith("pyc"):
                f = f[:-3] + "py"
            if n.find(".") == -1:
                add(dereference(f), os.path.basename(f), package.__name__)
            else:
                add(dereference(f), n + ".py", package.__name__)

        # include metadata in the same zip file
        metadata_path = metadata_to_add.get(package.__name__)
        if metadata_path is not None:
            add_files_for_package(metadata_path, metadata_path, os.path.basename(metadata_path), package.__name__)

    return archives_list


class SparkMixin():
    driver_memory = luigi.Parameter(
        config_path={'section': 'spark', 'name': 'driver-memory'},
        description='Memory for spark driver',
        significant=False,
    )
    executor_memory = luigi.Parameter(
        config_path={'section': 'spark', 'name': 'executor-memory'},
        description='Memory for each executor',
        significant=False,
    )
    executor_cores = luigi.Parameter(
        config_path={'section': 'spark', 'name': 'executor-cores'},
        description='No. of cores for each executor',
        significant=False,
    )
    spark_conf = luigi.Parameter(
        config_path={'section': 'spark', 'name': 'conf'},
        description='Spark configuration',
        significant=False,
        default=None
    )
    always_log_stderr = False  # log stderr if spark fails, True for verbose log


class BasicSparkJobTask(SparkMixin, PySparkTask):
    """
    Base class for running a launchable Spark task.
    """

    _spark = None
    _spark_context = None
    _tmp_dir = None
    log = None

    def init_spark(self, sc):
        """
        Initialize Spark, SQL and Hive context.
        :param sc: Spark context
        """
        from pyspark.sql import SparkSession
        self._spark_context = sc
        # Note that this doesn't actually use sc.  It just gets
        # the currently-existing Spark session.
        self._spark = SparkSession.builder.getOrCreate()

        self._tmp_dir = tempfile.mkdtemp()

        # TODO: pull definition of __name__ out into a class property.
        log4jLogger = sc._jvm.org.apache.log4j  # using spark logger
        self.log = log4jLogger.LogManager.getLogger(__name__)

    @property
    def conf(self):
        """Adds spark configuration to spark-submit task."""
        return self._dict_config(self.spark_conf)

    def spark_job(self):
        """Spark code for the job."""
        raise NotImplementedError

    def get_config_from_args(self, key, *args, **kwargs):
        """
        Returns `value` of `key` after parsing string argument.
        """
        default_value = kwargs.get('default_value', None)
        str_arg = args[0]
        config_dict = ast.literal_eval(str_arg)
        value = config_dict.get(key, default_value)
        return value

    def get_luigi_configuration(self):
        """
        Return Luigi configuration as dict for spark task.

        Luigi configuration cannot be retrieved directly from Luigi's get_config() method inside a Spark task.
        """
        return None

    def app_options(self):
        """
        List of options that needs to be passed to Spark task.

        Overrides empty SparkSubmitTask default.
        """
        options = {}
        task_config = self.get_luigi_configuration()  # load task dependencies first, if any.
        if isinstance(task_config, dict):
            options = task_config
        configuration = luigi.configuration.get_config()
        cluster_dependencies = configuration.get('spark', 'edx_egg_files', None)  # spark worker nodes dependency
        if cluster_dependencies is not None:
            options['cluster_dependencies'] = cluster_dependencies
        return [options]

    def _load_internal_dependency_on_cluster(self, *args):
        """
        Creates a zip of package and loads it on spark worker nodes.

        Loading via Luigi configuration does not work, as it creates a tar file, whereas Spark does not load tar files.
        """

        # Import packages to be loaded on cluster.
        # This list was taken from what was needed for Hadoop.  These may not all be needed still on Spark jobs.
        # Not all jobs need this, so perhaps the list of many packages can be moved to a derived class.
        import edx
        import luigi
        import opaque_keys
        import stevedore
        import bson
        import ccx_keys
        import cjson
        import boto
        import filechunkio
        import ciso8601
        import chardet
        import urllib3
        import certifi
        import idna
        import requests
        import six

        dependencies_list = []
        # get cluster dependencies from *args
        cluster_dependencies = self.get_config_from_args('cluster_dependencies', *args, default_value=None)
        if cluster_dependencies is not None:
            cluster_dependencies = json.loads(cluster_dependencies)
        if isinstance(cluster_dependencies, list):
            dependencies_list += cluster_dependencies

        packages = [edx, luigi, opaque_keys, stevedore, bson, ccx_keys, cjson, boto, filechunkio, ciso8601, chardet,
                    urllib3, certifi, idna, requests, six]
        dependencies_list += create_packages_archive(packages, self._tmp_dir)
        if len(dependencies_list) > 0:
            for file in dependencies_list:
                self._spark_context.addPyFile(file)

    def _clean(self):
        """Do any cleanup after job here"""
        if self._tmp_dir:
            shutil.rmtree(self._tmp_dir)

    def main(self, sc, *args):
        try:
            self.init_spark(sc)  # initialize spark contexts
            self._load_internal_dependency_on_cluster(*args)  # load packages on EMR cluster for spark worker nodes
            self.spark_job(*args)  # execute spark job
        finally:
            self._clean()  # cleanup after spark job
