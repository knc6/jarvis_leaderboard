# https://colab.research.google.com/github/knc6/jarvis-tools-notebooks/blob/master/jarvis-tools-notebooks/kgcnn_jarvis_leaderboard.ipynb
from kgcnn.literature.CGCNN import make_crystal_model
from tensorflow.keras.optimizers import Adam
from kgcnn.training.scheduler import LinearLearningRateScheduler
import kgcnn
import pandas as pd
import os
from datetime import timedelta
from copy import deepcopy
from kgcnn.metrics.metrics import ScaledMeanAbsoluteError
from kgcnn.data.transform.scaler.standard import StandardScaler
from jarvis.core.atoms import Atoms
from kgcnn.data.crystal import CrystalDataset
import numpy as np
from sklearn.metrics import mean_absolute_error
from jarvis.db.jsonutils import loadjson, dumpjson
from kgcnn.training.hyper import HyperParameter
from kgcnn.model.utils import get_model_class
import glob
import time

# If you want to deactivate cuda devices, in case tensorflow installation does not properly support cuda.
# Tensorflow must find visible cuda device. Please check tensorflow installation if problems occur.
# os.environ["CUDA_VISIBLE_DEVICES"] = "-1"

print("Check:", kgcnn.__kgcnn_version__, "vs. 3.0.1")
ragged = True
model_config = {
        'name': 'CGCNN',
        'inputs': [
            {'shape': (None,), 'name': 'node_number', 'dtype': 'int64', 'ragged': True},
            {'shape': (None, 3), 'name': 'node_frac_coordinates', 'dtype': 'float64', 'ragged': True},
            {'shape': (None, 2), 'name': 'range_indices', 'dtype': 'int64', 'ragged': True},
            {'shape': (3, 3), 'name': 'graph_lattice', 'dtype': 'float64', 'ragged': False},
            {'shape': (None, 3), 'name': 'range_image', 'dtype': 'float32', 'ragged': True},
            # For `representation="asu"`:
            # {'shape': (None, 1), 'name': 'multiplicities', 'dtype': 'float32', 'ragged': True},
            # {'shape': (None, 4, 4), 'name': 'symmops', 'dtype': 'float64', 'ragged': True},
        ],
        'input_embedding': {'node': {'input_dim': 95, 'output_dim': 64}},
        'representation': 'unit',  # None, 'asu' or 'unit'
        'expand_distance': True,
        'make_distances': True,
        'gauss_args': {'bins': 60, 'distance': 6, 'offset': 0.0, 'sigma': 0.4},
        'conv_layer_args': {
            'units': 128,
            'activation_s': 'kgcnn>shifted_softplus',
            'activation_out': 'kgcnn>shifted_softplus',
            'batch_normalization': True,
        },
        'node_pooling_args': {'pooling_method': 'mean'},
        'depth': 4,
        'output_mlp': {'use_bias': [True, True, False], 'units': [128, 64, 1],
                       'activation': ['kgcnn>shifted_softplus', 'kgcnn>shifted_softplus', 'linear']},
}

tasks = []
for i in glob.glob("../*/AI-SinglePropertyPrediction*test-mae.csv.zip"):
    if "formula" not in i:
        task = i.split("/")[-1].split(".csv.zip")[0]
        if task not in tasks:
            tasks.append(task)
print("tasks", tasks, len(tasks))

# For a quick test running on one task only
# tasks = ["AI-SinglePropertyPrediction-exfoliation_energy-dft_3d-test-mae"]

for task in tasks:
    t1 = time.time()
    zip_name = task + '.csv.zip'
    if not os.path.exists(zip_name):
        cmd = (
                "jarvis_populate_data.py --benchmark_file "
                + task
                + " --output_path=Out"
        )
        if not os.path.exists("Out"):
            print("Command to populate data: %s." % cmd)
            print(os.system(cmd))
        if not os.path.exists("Out"):
            print("WARNING: Failed to populate data.")
            continue

        dataset_info = loadjson("Out/dataset_info.json")
        n_train = dataset_info["n_train"]
        n_val = dataset_info["n_val"]
        n_test = dataset_info["n_test"]

        if n_train > 500000:
            print("Too large to train within 2 days.", n_train, task)
            # Clean up and continue.
            cmd = "rm -r Out"
            os.system(cmd)
            cmd = "rm -r exfoliation_en_train"
            os.system(cmd)
            continue

        def ensure_label_dim(y):
            if len(y.shape) <= 1:
                y = np.expand_dims(y, axis=-1)
            return y

        def prepare_data(dirname="exfoliation_en", populated_data_path="Out"):
            id_prop_path = os.path.join(populated_data_path, "id_prop.csv")
            df = pd.read_csv(id_prop_path, header=None)
            df.columns = ["id", "target"]

            # train
            train_dir = dirname + "_train"
            if not os.path.exists(train_dir):
                os.makedirs(train_dir)
            train_cif = os.path.join(train_dir, "CifFiles")
            if not os.path.exists(train_cif):
                os.makedirs(train_cif)
            csv_file = train_dir + "/data.csv"
            print("train_dir", train_dir)
            f = open(csv_file, "w")
            f.write("file_name,index,label\n")
            targets = []
            for i, ii in df.iterrows():
                pos_path = os.path.join(populated_data_path, ii["id"])
                atoms = Atoms.from_poscar(pos_path)
                pmg = atoms.pymatgen_converter()
                targets.append(ii["target"])
                fname = "file_" + str(ii["id"]) + ".cif"
                # fname="file_"+str(i)+".cif"
                cif_name = os.path.join(train_cif, fname)
                pmg.to(filename=cif_name, fmt="cif")
                line = fname + "," + str(i) + "," + str(ii["target"]) + "\n"
                f.write(line)
            f.close()
            dataset = CrystalDataset(
                data_directory=train_dir,
                dataset_name=train_dir,
                file_name="data.csv",
                file_directory="CifFiles",
            )
            dataset.prepare_data(file_column_name="file_name", overwrite=True)
            dataset.read_in_memory(label_column_name="label")
            labels = ensure_label_dim(np.array(dataset.get("graph_labels")))
            dataset.map_list(
                method="set_range_periodic", max_distance=6.0, max_neighbours=None
            )

            return dataset, labels, df, train_dir


        dataset, labels, df, train_dir = prepare_data(populated_data_path="Out")

        print("n_train", n_train)
        print("n_val", n_val)
        print("n_test", n_test)

        train_index = np.arange(0, n_train)
        val_index = np.arange(n_train, n_train + n_val)
        test_index = np.arange(n_train + n_val, n_train + n_val + n_test)

        # Prepare input data for model fit.
        dataset_train = dataset[train_index]
        dataset_val = dataset[val_index]
        dataset_test = dataset[test_index]
        # We can clean the dataset for training.
        # However, for validation or test this would impact the results.
        invalid_train = dataset_train.clean(model_config["inputs"])
        invalid_val = dataset_val.clean(model_config["inputs"])
        invalid_test = dataset_test.clean(model_config["inputs"])
        print("Invalid items:", invalid_train, invalid_val, invalid_test)

        # Map to tensor.
        x_train, y_train = (
            dataset_train.tensor(model_config["inputs"]),
            ensure_label_dim(np.array(dataset_train.get("graph_labels")))
        )
        x_val, y_val = (
            dataset_val.tensor(model_config["inputs"]),
            ensure_label_dim(np.array(dataset_val.get("graph_labels")))
        )
        x_test, y_test = (
            dataset_test.tensor(model_config["inputs"]),
            ensure_label_dim(np.array(dataset_test.get("graph_labels")))
        )
        y_train_ref, y_val_ref, y_test_ref = labels[train_index], labels[val_index], labels[test_index]
        # We use standard scaler to normalize regression targets.
        scaler = StandardScaler()
        y_train = scaler.fit_transform(y_train)
        y_val = scaler.transform(y_val)
        y_test = scaler.transform(y_test)
        scaled_mae_metric = ScaledMeanAbsoluteError(scaler.get_scaling().shape, name="scaled_mean_absolute_error")
        scaled_mae_metric.set_scale(scaler.get_scaling())

        # Making keras model.
        temp_config = deepcopy(model_config)
        model = make_crystal_model(**temp_config)
        # Compile model.
        model.compile(
            loss="mean_absolute_error",
            optimizer=Adam(learning_rate=5e-04),
            metrics=["mean_absolute_error", scaled_mae_metric],  # MAE for scaled and rescaled targets.
        )
        # Fit model.
        start = time.time()
        model.fit(
            x_train,
            y_train,
            callbacks=[
                LinearLearningRateScheduler(epo_min=10, epo=1000, learning_rate_start=5e-04, learning_rate_stop=1e-05)
            ],
            validation_data=(x_val, y_val),
            validation_freq=10,
            shuffle=True,
            batch_size=32,
            epochs=1000,
            verbose=2,
        )
        stop = time.time()
        time_taken_for_fit = stop-start
        print("Print Time for training: ", str(timedelta(seconds=stop - start)))

        # Predict validation and test labels.
        val_pred = model.predict(x_val, verbose=2)
        test_pred = model.predict(x_test, verbose=2)
        # Inverse scaling.
        val_pred = scaler.inverse_transform(val_pred)
        test_pred = scaler.inverse_transform(test_pred)
        # Fix invalid structures
        if invalid_val is not None:
            if len(invalid_val) > 0:
                val_pred = list(val_pred)
                for iter_invalid in reversed(sorted(list(invalid_val))):
                    val_pred.insert(iter_invalid, np.mean(y_train_ref, axis=0))
                val_pred = np.array(val_pred)
        if invalid_test is not None:
            if len(invalid_test) > 0:
                test_pred = list(test_pred)
                for iter_invalid in reversed(sorted(list(invalid_test))):
                    test_pred.insert(iter_invalid, np.mean(y_train_ref, axis=0))
                test_pred = np.array(test_pred)
        # Check predictions.
        print(
            "Error with reference values:",
            mean_absolute_error(y_val_ref, val_pred),
            mean_absolute_error(y_test_ref, test_pred),
        )
        df_test = df[-n_test:]
        csv_name = task + ".csv"
        f = open(csv_name, "w")
        f.write("id,prediction\n")
        for i in range(len(df_test)):
            # print (i)
            jid = df_test.iloc[i]["id"]
            target = df_test.iloc[i]["target"]
            # print(jid,target,y_test[i][0],test_pred[i][0])
            line = jid + "," + str(test_pred[i][0]) + "\n"
            f.write(line)
        f.close()

        cmd = "zip " + csv_name + ".zip " + csv_name
        os.system(cmd)

        time_dict = loadjson("timing.json")
        time_dict[csv_name + ".zip"] = time_taken_for_fit
        dumpjson(time_dict, "timing.json")

        cmd = "rm -r Out"
        os.system(cmd)
        cmd = "rm -r exfoliation_en_train"
        os.system(cmd)
        cmd = "rm " + csv_name
        # os.system(cmd)
        cmd = "rm -r " + train_dir
        os.system(cmd)

    t2 = time.time()
    print('Time', t2 - t1)
