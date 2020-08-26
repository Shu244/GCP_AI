import gcp_interactions as gcp
import argparse
import strings
import random
import torch
import copy
import time
import json
import os

from MNIST_test import run


'''
Best model and VM progress will save the same items:
-hyparameters, parameters, progress report

results will save one file containing:
-hyperparameters and progress report
'''

class Manager:
    '''
    Triggers saving the current state of the model, hyperparameters, and performance
    '''

    def __init__(self, temp_path, bucket_name, rank):
        self.rank = rank
        self.bucket_name = bucket_name
        self.temp_path = temp_path
        self.quick_send = gcp.QuickSend(temp_path, bucket_name)

        self.download_progress_folder(bucket_name, temp_path, rank)

        self.tracker = Tracker(self.quick_send, rank)
        self.hyparams = Hyperparameters(self.quick_send, rank)
        self.count = 0

    def download_progress_folder(self, bucket_name, tmp_folder, rank):
        folder_path = strings.vm_progress + ("/%d/" % rank)
        gcp.download_folder(bucket_name, folder_path, tmp_folder)

    def start_epoch(self):
        return self.tracker.start_epoch()

    def set_compare_goal(self, compare, goal):
        self.tracker.set_compare_goal(compare, goal)

    def get_hyparams(self):
        return self.hyparams.get_hyparams()

    def add_progress(self, key, value):
        self.tracker.add(key, value)

    def finished(self, param_dict):
        self.save_results()
        self.save_best(param_dict)
        self.reset()
        self.reset_cloud_progress()

    def reset_cloud_progress(self):
        '''
        Resets the cloud folder keeping track of progress by deleting the params and removing current hyperparameter
        values
        '''
        cloud_folder_path = os.path.join(strings.vm_progress, str(self.rank))
        gcp.delete_all_prefixes(self.bucket_name, cloud_folder_path)

        hyparams_copy = copy.deepcopy(self.hyparams.raw_hyparams)
        hyparams_copy.pop("current_values", None)

        self.quick_send.send(strings.vm_hyparams_report, json.dumps(hyparams_copy), cloud_folder_path)

    def reset(self):
        self.tracker.reset()
        self.hyparams.reset()

    def save_progress(self, param_dict):
        folder_path = strings.vm_progress + ("/%d" % self.rank)
        self.tracker.save_progress(folder_path)
        self.hyparams.save_hyparams(folder_path)
        self.save_params(param_dict, folder_path)

    def save_results(self):
        progress_report = self.tracker.get_report()
        hyparams_report = self.hyparams.get_raw_hyparams()

        timestr = time.strftime("%m%d%Y-%H%M%S")
        readable_timestr = time.strftime("%m/%d/%Y-%H:%M:%S")

        filename = timestr + ("-vm%d" % self.rank) + ('-%d' % self.count) + ".json"
        result = {
            "progress": progress_report,
            "hyperparameters": hyparams_report,
            "time": readable_timestr
        }
        msg = json.dumps(result)

        self.quick_send.send(filename, msg, strings.results + ("/%d" % self.rank))
        self.count += 1

    def save_best(self, param_dict):
        if self.isBest(self.tracker.get_report()):
            folder_path = strings.best_model + ("/%d" % self.rank)
            self.tracker.save_progress(folder_path)
            self.hyparams.save_hyparams(folder_path)
            self.save_params(param_dict, folder_path)
            return True
        return False

    def isBest(self, cur_report):
        report_path = os.path.join(strings.best_model, str(self.rank), strings.vm_progress_report)

        try:
            gcp.download_file(self.bucket_name, report_path, self.temp_path)
        except Exception as err:
            return True

        local_report_path = os.path.join(self.temp_path, strings.vm_progress_report)
        best_report = json.load(open(local_report_path))

        # metric to compare
        compare = cur_report["compare"]
        goal = cur_report["goal"]

        if goal == "max":
            best_val = max(best_report[compare])
            cur_val = max(cur_report[compare])
            if cur_val > best_val:
                return True
        else:
            best_val = min(best_report[compare])
            cur_val = min(cur_report[compare])
            if cur_val < best_val:
                return True
        return False

    def save_params(self, param_dict, cloud_folder):
        '''
        Saving the parameters for a model to a folder determined by whether or not the training is done.

        :param param_dict: Generated by model.state_dict()
        '''
        local_path = os.path.join(self.temp_path, strings.params_file)
        torch.save(param_dict, local_path)
        gcp.upload_file(self.bucket_name, local_path, cloud_folder)


class Tracker:
    '''
    Used to track the performance of a model while training:
    -Loads and saves model progress
    '''

    def __init__(self, quick_send, rank):
        self.quick_send = quick_send
        self.progress_report_local_pth = os.path.join(
            quick_send.temp_path,
            strings.vm_progress_report)
        if os.path.isfile(self.progress_report_local_pth):
            self.report = json.load(open(self.progress_report_local_pth))
        else:
            self.reset()
        self.rank = rank

    def add(self, key, value):
        if key not in self.report:
            self.report[key] = []
        self.report[key].append(value)

    def save_progress(self, folder):
        self.quick_send.send(strings.vm_progress_report, json.dumps(self.report), folder)

    def get_report(self):
        return self.report

    def reset(self):
        self.report = {
            "goal": "max",
            "compare": "val_accuracy"
        }

    def set_compare_goal(self, compare, goal):
        self.report["compare"] = compare
        self.report["goal"] = goal

    def start_epoch(self):
        dict_keys = list(self.report.keys())
        dict_keys.remove("compare")
        dict_keys.remove("goal")
        if len(dict_keys) == 0:
            return 0
        return len(self.report[dict_keys[0]])


class Hyperparameters:
    '''
    Manages the hyperparameters:
    -Loads hyperparameters and saves them
    '''

    def __init__(self, quick_send, rank):
        file_path = os.path.join(quick_send.temp_path, strings.vm_hyparams_report)
        self.raw_hyparams = json.load(open(file_path))
        self.cur_val = "current_values"
        self.quick_send = quick_send
        self.rank = rank

        if self.cur_val in self.raw_hyparams and self.raw_hyparams[self.cur_val] != None:
            self.load_params = True
        else:
            # generate new values and sets self.load_params = False
            self.generate()

    def reset(self):
        self.generate()
        self.raw_hyparams["current_iter"] = self.raw_hyparams["current_iter"] + 1

    def generate(self):
        '''
        Generates new hyperparameters according to specifications in hyparam_obj
        '''

        hyparam_copy = copy.deepcopy(self.raw_hyparams["hyperparameters"])
        for key, value in hyparam_copy.items():
            if isinstance(value, list):
                new_val = random.uniform(value[0], value[1])
                hyparam_copy[key] = new_val
        self.raw_hyparams[self.cur_val] = hyparam_copy
        self.load_params = False

    def get_hyparams(self):
        return self.raw_hyparams[self.cur_val]

    def get_raw_hyparams(self):
        '''
        The raw hyperparameter dictionary contains the current hyperparemter values as
        well as the information specifying the portion of the hyperparameter grid being searched.

        :return: Raw hyperparameter dictionary
        '''

        return self.raw_hyparams

    def save_hyparams(self, cloud_folder):
        self.quick_send.send(strings.vm_hyparams_report, json.dumps(self.raw_hyparams), cloud_folder)


def hyparam_search(manager):
    start = manager.hyparams.raw_hyparams["current_iter"]
    end = manager.hyparams.raw_hyparams["max_iter"]
    quick_send = manager.quick_send
    rank = manager.rank
    temp_path = manager.temp_path

    while start < end:
        try:
            param_pth = os.path.join(temp_path, strings.params_file) if manager.hyparams.load_params else None
            run(manager, param_pth)
        except Exception as err:
            timestr = time.strftime("%m%d%Y-%H%M%S")
            readable_timestr = time.strftime("%m/%d/%Y-%H:%M:%S")
            filename = timestr + ("-vm%d" % rank) + "-iter" + str(start) + ".json"
            msg = {
                "error": str(err),
                "hyperparameters": manager.get_hyparams(),
                "progress": manager.tracker.get_report(),
                "time": readable_timestr
            }
            msg = json.dumps(msg)
            quick_send.send(filename, msg, strings.shared_errors)
            print("Writing the following msg to shared errors folder in Google cloud")
            print(msg)
        start += 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Training model over a portion of the hyperparameters")

    parser.add_argument('rank', help='The id for this virtual machine', type=int)
    parser.add_argument('bucket_name', help='The name of the bucket')
    parser.add_argument("-m", '--tmppth', default="./tmp", help='The folder to store temporary files before moving to gcloud')

    args = parser.parse_args()

    manager = Manager(args.tmppth, args.bucket_name, args.rank)
    hyparam_search(manager)
