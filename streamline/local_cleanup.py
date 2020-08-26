import gcp_interactions as gcp
import argparse
import strings
import copy
import time
import json


def make_bucket(bucket_name, location):
    print(
        '''
        Making a bucket: 
        -bucket name: {0}
        -location: {1}
        '''.format(bucket_name, location))

    gcp.make_bucket(bucket_name, location)


def move_data(bucket_name, data_path):
    print(
        '''
        Moving data to bucket: 
        -bucket name: {0}
        -data_path: {1}
        '''.format(bucket_name, data_path))

    exts_list = data_path.split('.')
    if len(exts_list) < 3 or exts_list[-1] != 'gz' or exts_list[-2] != 'tar':
        raise ValueError("Data path must be archived (.tar) and compressed (.zip)")

    gcp.upload_file(bucket_name, data_path, strings.data)


def move_access_token(bucket_name, access_token_pth):
    print(
        '''
        Moving token to bucket: 
        -bucket name: {0}
        -access_token_path: {1}
        '''.format(bucket_name, access_token_pth))

    gcp.upload_file(bucket_name, access_token_pth, strings.secrets)


def rmvms(bucket_name, folder_name):
    folder_name = 'VM-progress'

    print(
        '''
        Removing folder containing VM progress. 
        -bucket name: {0}
        -folder_name: {1}
        '''.format(bucket_name, folder_name))

    gcp.delete_all_prefixes(bucket_name, folder_name)


def fill(big, small):
    big_copy = copy.deepcopy(big)
    for key in big_copy:
        if key in small:
            big_copy[key] = small[key]
    return big_copy


def hyperparamters(bucket_name, hyparams_path, quick_send):
    print(
        '''
        Removing error folder and recreating progress folder to hold new hyperparameters. 
        -bucket name: {0}
        -hyparams_pth: {1}
        '''.format(bucket_name, hyparams_path))

    gcp.delete_all_prefixes(bucket_name, strings.vm_progress)
    gcp.delete_all_prefixes(bucket_name, strings.shared_errors)

    hyparam_configs = json.load(open(hyparams_path))
    iters = hyparam_configs["iterations"]
    all_hyperparameters = hyparam_configs["hyperparameters"]
    first = all_hyperparameters[0]
    for index, hyperparameters in enumerate(all_hyperparameters):
        if index == 0:
            filled = hyperparameters
        else:
            filled = fill(first, hyperparameters)
        wrapper = {}
        wrapper["hyperparameters"] = filled
        wrapper["current_iter"] = 0
        wrapper["max_iter"] = iters
        quick_send.send(strings.vm_hyparams_report, json.dumps(wrapper), strings.vm_progress + '/' + str(index))


def build_cluster(project_id, bucket_name, workers, machine_configs_pth, startup_script_pth, quick_send):
    print(
        '''
        Building cluster for training
        -project_id: {0}
        -workers: {1}
        -machine_configs_pth: {2}
        -startup_script_pth: {3}
        -bucket_name: {4}
        '''.format(project_id, workers, machine_configs_pth, startup_script_pth, bucket_name))

    max_workers = workers
    machine_configs = json.load(open(machine_configs_pth))
    startup_script = open(startup_script_pth, 'r').read()

    valid_zones = machine_configs['zones']
    remaining_ranks = list(range(0, workers))

    '''
    Builds VM according to configs with given startup script.
    If VM fails to build, then the zone is removed and we try a different zone. 
    Cycle continues until there are no more zones or all workers have been built
    '''
    while workers > 0 and len(valid_zones) > 0:
        unused_ranks = []
        operations = []
        for idx, rank in enumerate(remaining_ranks):
            zone = valid_zones[idx % len(valid_zones)]
            try:
                operation = gcp.create_instance(project_id, machine_configs, startup_script,
                                                zone, rank, bucket_name)
                operation['vm-rank'] = rank
                operation['testing-zone'] = zone
                operations.append(operation)
            except Exception:
                print("Failed to build a worker in zone %s, trying a different zone" % zone)
                unused_ranks.append(rank)
                valid_zones.remove(zone)

        for op in operations:
            zone = op['testing-zone']
            rank = op['vm-rank']
            passed = gcp.wait_for_operation(project_id, op['name'], zone)
            if passed:
                workers -= 1
            else:
                print("Failed to build a worker in zone %s, trying a different zone" % zone)
                unused_ranks.append(rank)
                valid_zones.remove(zone)
        remaining_ranks = unused_ranks

    # Writing errors to bucket
    if workers != 0:
        print("Writing error to shared errors in the cloud")
        error_msg = "%d/%d workers built. All desired hyperparemters could not be explored" % \
              (max_workers - len(remaining_ranks), max_workers)
        msg = {
            "error": error_msg,
            "time": time.strftime("%m/%d/%Y-%H:%M:%S")
        }
        quick_send.send(strings.cluster_error, json.dumps(msg), strings.shared_errors)

    print("%d/%d workers built" % (max_workers - len(remaining_ranks), max_workers))


def gen_bucket_name(project_id, bucket_name):
    return project_id + '-' + bucket_name


def hr():
    print('----'*20)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Prepping buckets and spinning up VMs to train model.")

    parser.add_argument('project_id', help='Project ID')
    parser.add_argument('bucket_name', help='The name of the bucket')
    parser.add_argument("-c", '--cluster', nargs=3,
                        help='Build VM cluster for training. Requires number of workers, machine configs path, and startup script path')
    parser.add_argument("-t", '--tokenpth', help='The access_token path is used to download private repo from GitHub')
    parser.add_argument("-b", "--mkbucket", action="store_true", help="Create the bucket")
    parser.add_argument("-d", "--datapth", help="The path of the data to move into the bucket")
    parser.add_argument("-p", "--hyparams", help="The path for the hyperparameter json")
    parser.add_argument("-l", "--location", default="us-central1", help="The location for your bucket")
    parser.add_argument("-m", '--tmppth', default="./tmp", help='The folder to store temporary files before moving to gcloud')

    args = parser.parse_args()

    pid = args.project_id
    bname = gen_bucket_name(pid, args.bucket_name)
    quick_send = gcp.QuickSend(args.tmppth, bname)

    if args.mkbucket:
        make_bucket(bname, args.location)
        hr()

    if args.tokenpth:
        move_access_token(bname, args.tokenpth)
        hr()

    if args.datapth:
        move_data(bname, args.datapth)
        hr()

    if args.hyparams:
        hyperparamters(bname, args.hyparams, quick_send)
        hr()

    if args.cluster:
        push_latest_code = input(
            "VMs will pull from your github, is the desired version the latest commit on master? [yes | no]")
        if push_latest_code.lower() not in ["yes", "y"]:
            print("Please push your desired code before continuing")
            exit(0)

        num_worker = int(args.cluster[0])
        machine_configs_pth = args.cluster[1]
        startup_script_pth = args.cluster[2]
        build_cluster(pid, bname, num_worker, machine_configs_pth, startup_script_pth, quick_send)
        hr()
