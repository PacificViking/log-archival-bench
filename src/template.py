import pathlib
import subprocess
import logging
import os
import yaml
import time
import threading
import datetime
import sys
import json
import shlex
import uuid
import inspect
from src.jsonsync import JsonItem

WORK_DIR = "/home"
ASSETS_DIR = f"{WORK_DIR}/assets"
DATASETS_DIR = f"{WORK_DIR}/datasets"

logger = logging.getLogger(__name__)
logger.propagate = False
#logger.setLevel(logging.DEBUG)
logger.setLevel(logging.INFO)
logging_console_handler = logging.StreamHandler()
logging_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
logging_console_handler.setFormatter(logging_formatter)
logger.addHandler(logging_console_handler)

def append_memory(self):
    kb_to_b = 1024
    output = self.docker_execute("ps aux").split('\n')
    metric_sample = 0
    for line in output:
        process = line.strip().split()[10].strip()
        for related_process in self.config["related_processes"]:
            if related_process.startswith(process):
                metric_sample += int(line.strip().split()[5]) * kb_to_b
                break
    #logger.info(f"Memory used: {metric_sample//(1024*1024)} MB")
    self.bench_info['memory'].append(metric_sample)

def append_docker_memory(self):
    metric_sample = 0
    for jsonline in subprocess.check_output(["docker", "stats", "--no-stream", "--no-trunc", "--format", "json"]).decode().split('\n'):
        if not jsonline.strip():
            continue
        line = json.loads(jsonline)
        for prefix in self.config["container_prefixes"]:
            if line["Name"].startswith(prefix):
                memtxt = line["MemUsage"].split()[0]
                if memtxt[:-1].isdigit():   # B
                    metric_sample += int(memtxt[-1])
                else:
                    memunit = memtxt[-3]
                    memnum = float(memtxt[:-3])
                    memunitmult = {'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4}[memunit]
                    metric_sample += memnum * memunitmult
                break
    #logger.info(f"Memory used: {metric_sample//(1024*1024)} MB")
    self.bench_info['memory'].append(metric_sample)


def poll_memory(self, bench_uuid):
    while True:
        if self.bench_info['ingest'] is True:
            interval = self.config["system_metric"]["memory"]["ingest_polling_interval"]
        else:
            interval = self.config["system_metric"]["memory"]["run_query_benchmark_polling_interval"]
        time.sleep(interval - (time.time() % interval))  # wait for next "5 second interval"

        if self.bench_info['running'] == bench_uuid:
            if "measure_docker_memory" in self.config and self.config["measure_docker_memory"] is True:
                append_docker_memory(self)
            else:
                append_memory(self)
        else:
            break

class Benchmark:
    def __init__(self, dataset_dir, dataset_variation='normal_log'):
        with open(f"{self.script_dir}/config.yaml") as file:
            self.config = yaml.safe_load(file)

        with open(f"{dataset_dir}/metadata.yaml") as file:
            self.dataset_meta = yaml.safe_load(file)

        self.queries = self.config["queries"]

        self.dataset = os.path.abspath(dataset_dir)
        self.dataset_name = os.path.basename(self.dataset)

        self.outputjson = f"{self.script_dir}/output.json"
        assert os.path.exists(f"{self.script_dir}/Dockerfile")
        assert os.path.exists(f"{self.script_dir}/config.yaml")

        self.output = JsonItem.read(self.outputjson)

        self.bench_info = {}
        self.attach = False

        self.datasets_path = f"{DATASETS_DIR}/{self.dataset_meta[dataset_variation]}"  # inside container
        self.datasets_path_in_host = f"{self.dataset}/{self.dataset_meta[dataset_variation]}"  # outside container

        if dataset_variation == "normal_log":
            self.properties = {}
        else:
            self.properties = {"dataset_variation": dataset_variation}


    def __init_subclass__(cls, **kwargs):  # hackery for script_dir so that it finds the assets dir
        super().__init_subclass__(**kwargs)
        # This gets the filename where the subclass is defined
        frame = inspect.stack()[1]
        cls.source_file = os.path.abspath(frame.filename)

    @property
    def container_name(self):
        return self.config['container_id']

    @property
    def script_dir(self):
        return pathlib.Path(type(self).source_file).parent.resolve()
        #return pathlib.Path(sys.argv[0]).parent.resolve()

    def get_disk_usage(self, path):
        try:
            return int(self.docker_execute(f'du {path} -bc').split('\n')[-1].split('\t')[0])
        except subprocess.CalledProcessError:
            return 0

    def check_results(self, ind, res):
        return (int(res) == [38611, 336293, 1, 122, 52421, 38611][ind])

    @property
    def decompressed_size(self):
        return self.get_disk_usage(self.datasets_path)

    @property
    def compressed_size(self):
        raise NotImplementedError

    @property
    def mount_points(self):
        return {}

    def wait_for_port(self, port_num, waitclose=False, timeout=180):
        starttime = time.time()

        try:
            self.docker_execute("which nc")
        except subprocess.CalledProcessError:
            raise Exception("nc not found in container")

        while True:
            try:
                self.docker_execute(f"nc -z localhost {port_num}")
                if waitclose:
                    time.sleep(1)
                else:
                    break
            except subprocess.CalledProcessError:
                if waitclose:
                    break
                else:
                    time.sleep(1)
            if timeout is not None and timeout > 0:
                if time.time() - starttime > timeout:
                    raise Exception('timeout')

    def docker_build(self):
        result = subprocess.run(
                f'docker build --tag {self.container_name} {self.script_dir}',
                shell = True,
                check = True
                )
        logger.debug(result)

    def docker_attach(self):
        result = subprocess.run(
                f'docker exec -it {self.container_name} bash',
                shell = True,
                check = True
                )
        logger.debug(result)

    @property
    def limits_param(self):
        return [
                '--cpus=4',
                #'--memory=8g',
                #'--memory-swap=8g'
                ]

    def docker_run(self, background=True):
        mount_param = [
                f'--mount "type=bind,src={key},dst={value}"'
                for key, value in self.mount_points.items()
                ]

        if background:
            interactive_param = [
                    '-d'
                    ]
            interactive_exec = 'bash'
        else:
            interactive_param = [
                    '-it',
                    f'--workdir {WORK_DIR}'
                    ]
            interactive_exec = f'bash -c "cd {WORK_DIR} && /bin/bash -l"'

        result = subprocess.run(
                ' '.join([
                    'docker',
                    'run',
                    '--privileged',
                    '--rm',
                    '--init',
                    '-it',
                    '--network host',
                    f'--workdir {WORK_DIR}',
                    f'--name {self.container_name}',
                    f'--mount "type=bind,src={self.script_dir},dst={ASSETS_DIR}"',
                    f'--mount "type=bind,src={self.dataset},dst={DATASETS_DIR}"',
                    *mount_param,
                    *self.limits_param,
                    *interactive_param,
                    f"{self.container_name}",
                    interactive_exec,
                ]),
                shell = True,
                check = True
                )
        logger.debug(result)

    def docker_remove(self, check=True):
        result = subprocess.run(
                f'docker container stop {self.container_name}',
                shell = True,
                check = check,
                stdout=subprocess.DEVNULL
                )
        logger.debug(result)
        while True:
            try:
                subprocess.run(
                    f'docker exec {self.container_name} echo www',
                    shell = True,
                    check = True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                    )
                time.sleep(1)
            except subprocess.CalledProcessError:
                break
        result = subprocess.run(
                f'docker container rm {self.container_name}',
                shell = True,
                check = False,
                stdout=subprocess.DEVNULL
                )
        time.sleep(10)

    def docker_execute(self, statement, check=True, background=False, output_stderr=True, shell=False):
        if output_stderr:
            stderr_redirect = subprocess.STDOUT
        else:
            stderr_redirect = subprocess.DEVNULL

        if background:
            background_flags = ['-d']
        else:
            background_flags = []

        if type(statement) is str:
            pass
        if type(statement) is list:
            statement = ' '.join(statement)
        #cmd = ['docker', 'exec', self.container_name, 'bash', '-c', *shlex.split(statement)]
        if shell:
            cmd = ['docker', 'exec', *background_flags, self.container_name, 'bash', '-c', statement]
        else:
            cmd = ['docker', 'exec', *background_flags, self.container_name, *shlex.split(statement)]
        
        result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=stderr_redirect,
                #shell = True,
                check = check
                )
        #logger.debug(result)
        if background:
            return "nothing"
        else:
            return result.stdout.decode("utf-8").strip()

    def ingest(self):
        raise NotImplementedError
    def search(self, query):
        raise NotImplementedError
    def clear_cache(self):
        raise NotImplementedError
    def reset(self):
        raise NotImplementedError
    def launch(self):
        raise NotImplementedError

    @property
    def terminate_procs(self):
        return []

    def terminate(self):
        for procname in self.terminate_procs:
            self.docker_execute(f"pkill -f {procname}", check=False)

    def bench_start(self, ingest=True):
        self.bench_info['ingest'] = ingest
        self.bench_info['memory'] = []

        bench_uuid = str(time.time())
        self.bench_info['running'] = bench_uuid

        self.bench_thread = threading.Thread(
                target = poll_memory,
                args = (self, bench_uuid)
                )
        self.bench_thread.daemon = True

        self.bench_thread.start()
        self.bench_info['start_time'] = time.time()

    def bench_stop(self):
        self.bench_info['end_time'] = time.time()
        self.bench_info['running'] = None
        self.bench_thread.join(timeout=0.01)

        try:
            self.bench_info['memory_average'] = sum(self.bench_info['memory']) / len(self.bench_info['memory'])
        except ZeroDivisionError:
            self.bench_info['memory_average'] = -1  # not even 5 seconds: no benchmark

        logger.info(f"Memory average: {self.bench_info['memory_average'] / 1024 / 1024} MiB")

        self.bench_info['time_taken'] = self.bench_info['end_time'] - self.bench_info['start_time']

    def bench_ingest(self):
        self.launch()
        self.reset()

        self.bench_start(ingest=True)
        self.ingest()
        self.bench_stop()
        
        self.output[self.dataset_name][json.dumps(self.properties)]['ingest'] = {
                'time_taken_s': self.bench_info['time_taken'],
                'memory_average_B': self.bench_info['memory_average'],
                'compressed_size_B': self.compressed_size,
                'decompressed_size_B': self.decompressed_size,
                'start_time': datetime.datetime.fromtimestamp(
                    self.bench_info['start_time']
                    ).strftime('%Y-%m-%d %H:%M:%S')
                }
        self.output.write()

        self.terminate()

    def bench_search(self, cold=True):
        self.launch()

        if self.compressed_size == 0:
            raise Exception("compressed size is 0: nothing to search")

        mode = "query_" + ("cold" if cold else "hot")
        for ind, query in enumerate(self.queries):
            
            self.clear_cache()

            if not cold:
                for _ in range(self.config["hot_run_warm_up_times"]):
                    self.search(query)

            self.bench_start(ingest=False)
            res = self.search(query)
            self.bench_stop()

            logger.info(f"Query #{ind} returned {res} results.")

            if not self.check_results(ind, res):
                logger.warning("The above result is inconsistent with previous results.")

            self.output[self.dataset_name][json.dumps(self.properties)][mode][ind] = {
                'time_taken_s': self.bench_info['time_taken'],
                'memory_average_B': self.bench_info['memory_average'],
                'result': res,
                'start_time': datetime.datetime.fromtimestamp(
                    self.bench_info['start_time']
                    ).strftime('%Y-%m-%d %H:%M:%S')
                }
            self.output.write()

        self.terminate()

    def print(self):
        print(self.output[self.dataset_name])

    def run_everything(self, run=['ingest', 'cold', 'hot']):
        logger.info("Removing possible previous container...")
        self.docker_remove(check=False)
        logger.info("Building container...")
        self.docker_build()
        logger.info("Running container...")
        self.docker_run(background=True)
        for i in run:
            if i == 'ingest':
                logger.info("Benchmarking ingestion...")
                self.bench_ingest()
            elif i == 'cold':
                logger.info("Benchmarking cold search...")
                self.bench_search(cold=True)
            elif i == 'hot':
                logger.info("Benchmarking hot search...")
                self.bench_search(cold=False)
        if self.attach:
            logger.info("Launching for attach...")
            self.launch()
            logger.info("Attaching to container...")
            self.docker_attach()
            logger.info("Terminating after attach...")
            self.terminate()
            logger.info("Removing container...")
            self.docker_remove()
        else:
            logger.info("Removing container...")
            self.docker_remove()

    def run_ingest(self):
        self.run_everything(['ingest'])

    def run_applicable(self, dataset_name):
        if dataset_name == "mongod":
            self.run_everything()
        else:
            self.run_ingest()
