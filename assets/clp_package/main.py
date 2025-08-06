#!/usr/bin/env python3

import sys
from src.template import WORK_DIR, Benchmark, logger
import os
import subprocess

CLP_PRESTO_CONTAINER_STORAGE = "/home/clp-json-x86_64"
CLP_PRESTO_HOST_STORAGE = os.path.abspath(os.path.expanduser("~/clp-json-x86_64-v0.4.0-dev"))
SQL_PASSWORD = "wqEGPyBdx_w"
HOST_IP = "127.0.0.1"
class clp_package_bench(Benchmark):
    def __init__(self, dataset):
        super().__init__(dataset)

        timestamp_key = self.dataset_meta['timestamp'].replace("$", r"\$")

        logger.info(f"timestamp_key: {timestamp_key}")

        self.timestamp = timestamp_key

    @property
    def compressed_size(self):
        return self.get_disk_usage(f"{CLP_PRESTO_CONTAINER_STORAGE}/var/data/archives/default")

    @property
    def mount_points(self):
        return {
            CLP_PRESTO_HOST_STORAGE: CLP_PRESTO_CONTAINER_STORAGE,
        }

    def launch(self):
        os.system(f"{CLP_PRESTO_HOST_STORAGE}/sbin/stop-clp.sh -f")
        os.chdir(CLP_PRESTO_HOST_STORAGE)
        os.system(r"""sed -i 's/^\([[:space:]]*\)"docker", "run",/\1"docker", "run", "--cpuset-cpus", "0-3",/g' """
                  + f"{CLP_PRESTO_HOST_STORAGE}/lib/python3/site-packages/clp_package_utils/scripts/start_clp.py")

        os.system(f"{CLP_PRESTO_HOST_STORAGE}/sbin/start-clp.sh")

    
    def sql_execute(self, query, check=True):
        if query[-1] != ';':
            query = query+';'
        return self.docker_execute(f"mysql -h {HOST_IP} -P 6001 -u clp-user -p{SQL_PASSWORD} -e '{query}' clp-db", check)

    def ingest(self):
        """
        Ingests the dataset at self.datasets_path
        """
        os.system(f'{CLP_PRESTO_HOST_STORAGE}/sbin/compress.sh --timestamp-key {self.timestamp} {self.datasets_path_in_host}')
        self.sql_execute(f"UPDATE clp_datasets SET archive_storage_directory=\"{CLP_PRESTO_CONTAINER_STORAGE}/var/data/archives/default\" WHERE name=\"default\"")

    def search(self, query):
        """
        Searches an already-ingested dataset with query, which is populated within config.yaml
        """
        res = subprocess.check_output([f'{CLP_PRESTO_HOST_STORAGE}/sbin/search.sh', query]).decode().strip()
        if not res:
            return 0
        return res.count('\n') + 1

    def clear_cache(self):
        pass
        #self.docker_execute("sync")
        #self.docker_execute("echo 1 >/proc/sys/vm/drop_caches", check=False, shell=True)

    def reset(self):
        os.system(f"{CLP_PRESTO_HOST_STORAGE}/sbin/stop-clp.sh -f")
        self.docker_execute(f'rm -r {CLP_PRESTO_CONTAINER_STORAGE}/var/data')
        os.system(f"{CLP_PRESTO_HOST_STORAGE}/sbin/start-clp.sh")

    def terminate(self):
        os.system(f"{CLP_PRESTO_HOST_STORAGE}/sbin/stop-clp.sh -f")
        os.chdir(CLP_PRESTO_HOST_STORAGE)
        os.system(r"""sed -i 's/^\([[:space:]]*\)"docker", "run", "--cpuset-cpus", "0-3",/\1"docker", "run",/g' """
                  + f"{CLP_PRESTO_HOST_STORAGE}/lib/python3/site-packages/clp_package_utils/scripts/start_clp.py")

def main():
    bench = clp_package_bench(sys.argv[1])
    bench.run_everything()

if __name__ == "__main__":
    main()
