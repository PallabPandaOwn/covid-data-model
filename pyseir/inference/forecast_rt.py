import math
from datetime import datetime, timedelta
import numpy as np
import logging
import pandas as pd
from matplotlib import pyplot as plt
import us
import structlog
from pyseir.utils import AggregationLevel, TimeseriesType
from pyseir.utils import get_run_artifact_path, RunArtifact
from structlog.threadlocal import bind_threadlocal, clear_threadlocal, merge_threadlocal
from structlog import configure
from enum import Enum

from tensorflow import keras
from sklearn import preprocessing
from keras.models import Sequential
from keras.layers import *
from keras.callbacks import EarlyStopping

configure(processors=[merge_threadlocal, structlog.processors.KeyValueRenderer()])
log = structlog.get_logger(__name__)


class ForecastRt:
    """
    Write doc string
    """

    def __init__(self):
        self.ref_date = datetime(year=2020, month=1, day=1)

        # Variable Names
        self.sim_date_name = "sim_day"
        self.predict_variable = "Rt_MAP__new_cases"
        self.forecast_variables = [
            "sim_day",
            "raw_new_cases",
            "raw_new_deaths",
            "Rt_MAP__new_cases",
        ]
        self.scaled_variable_suffix = "_scaled"

        # Seq2Seq Parameters
        self.sequence_length = 300
        self.mask_value = -10
        self.min_number_of_days = 30
        self.sample_train_length = 30  # Set to -1 to use all historical data
        self.predict_days = 3
        self.train_size = 0.8
        self.n_batch = 1
        self.n_epochs = 1
        self.n_hidden_dimensions = 100
        self.dropout = 0.01

    def get_scaling_dictionary(self, train_scaling_set):
        log.info("getting scaling dictionary")
        scalers_dict = {}
        for columnName, columnData in train_scaling_set.iteritems():
            scaler = preprocessing.MinMaxScaler(feature_range=(-1, 1))
            reshaped_data = columnData.values.reshape(-1, 1)

            scaler = scaler.fit(reshaped_data)
            scaled_values = scaler.transform(reshaped_data)

            scalers_dict.update({columnName: scaler})

        return scalers_dict

    def get_scaled_X_Y(self, samples, scalers_dict, PREDICT_DAYS, PREDICT_VARIABLE, MASK_VALUE):
        sample_list = list()
        for sample in samples:
            for columnName, columnData in sample.iteritems():
                scaled_values = scalers_dict[columnName].transform(columnData.values.reshape(-1, 1))
                sample.loc[:, f"{columnName}{self.scaled_variable_suffix}"] = scaled_values
            sample_list.append(sample)

        X, Y, df_list = self.get_X_Y(
            sample_list, self.predict_days, self.predict_variable, self.mask_value
        )
        return X, Y, df_list

    def forecast_rt(self, df_all):
        logging.info("starting")
        """
        predict r_t for 14 days into the future
        
        Parameters
        ___________
        df_all: dataframe with dates, new_cases, new_deaths, and r_t values

        Potential todo: add more features

        Returns
        __________
        dates and forecast r_t values

        """
        logging.info("beginning forecast")

        # Convert dates to what day of 2020 it corresponds to for Forecast

        df_all[self.sim_date_name] = (
            df_all.index - self.ref_date
        ).days + 1  # set first day of year to 1 not 0

        df_forecast = df_all[FORECAST_VARIABLES].copy()

        # Fill empty values with zero
        df_forecast.replace(r"\s+", self.mask_value, regex=True).replace("", self.mask_value)
        df_forecast.replace(np.nan, self.mask_value, regex=True).replace(np.nan, self.mask_value)

        df_forecast.to_csv("df_forecast.csv")  # , na_rep="NaN")

        # Split into train and test before normalizing to avoid data leakage
        # TODO: Test set will actually be entire series
        df_samples = self.create_df_list(df_forecast, MIN_NUMBER_OF_DAYS, PREDICT_DAYS)

        train_set_length = int(len(df_samples) * TRAIN_SIZE)
        train_scaling_set = df_samples[train_set_length]
        train_samples = df_samples[:train_set_length]
        test_samples = df_samples[train_set_length + 1 :]

        scalers_dict = self.get_scaling_dictionary(train_scaling_set)

        train_X, train_Y, train_df_list = self.get_scaled_X_Y(
            train_samples, scalers_dict, PREDICT_DAYS, PREDICT_VARIABLE, MASK_VALUE
        )
        test_X, test_Y, test_df_list = self.get_scaled_X_Y(
            test_samples, scalers_dict, PREDICT_DAYS, PREDICT_VARIABLE, MASK_VALUE
        )

        log.info(f"train samples: {len(train_X)} {len(train_df_list)}")
        log.info(f"test samples: {len(test_X)} {len(test_df_list)}")

        """
        for n in range(len(test_X)):
          log.info(f'------------------------   {n} ----------------')
          if n == 0 or n == 1 or n == len(test_X)-2 or n== len(test_X)-1:
              log.info('----------------------')
              log.info('train X')
              log.info(train_X[n])
              log.info('train Y')
              log.info(train_Y[n])
              log.info('DF')
              log.info(train_df_list[n])
        exit()


        for i, j, k in zip(test_X, test_Y, test_df_list):
            log.info('----------------------')
            log.info('test X')
            log.info(i)
            log.info('test Y')
            log.info(j)
            log.info('DF')
            log.info(k)
        """

        model, history = self.build_model(
            MASK_VALUE, n_epochs, n_batch, hidden_dimension, dropout, train_X, train_Y
        )

        logging.info("built model")

        # Plot predictions for test and train sets

        """
        logging.info("forecasts")
        logging.info(forecasts)
        logging.info("dates")
        logging.info(dates)
        """

        forecasts_train = list()
        dates_train = list()
        for i, j, k in zip(train_X, train_Y, train_df_list):
            # original_df = self.get_reshaped_X(i, n_batch, X_scaler)
            i = i.reshape(n_batch, i.shape[0], i.shape[1])
            scaled_df = pd.DataFrame(np.squeeze(i))
            thisforecast = scalers_dict[PREDICT_VARIABLE].inverse_transform(
                model.predict(i, batch_size=n_batch)
            )
            forecasts_train.append(thisforecast)

            last_train_day = np.array(scaled_df.iloc[-1][0]).reshape(1, -1)

            unscaled_first_test_day = (
                int(scalers_dict[SIM_DATE_NAME].inverse_transform(last_train_day)) + 1
            )

            predicted_days = np.arange(
                unscaled_first_test_day, unscaled_first_test_day + PREDICT_DAYS
            )
            dates_train.append(predicted_days)

        forecasts_test = list()
        dates_test = list()
        for i, j, k in zip(test_X, test_Y, test_df_list):
            # original_df = self.get_reshaped_X(i, n_batch, X_scaler)
            i = i.reshape(n_batch, i.shape[0], i.shape[1])
            scaled_df = pd.DataFrame(np.squeeze(i))
            thisforecast = scalers_dict[PREDICT_VARIABLE].inverse_transform(
                model.predict(i, batch_size=n_batch)
            )
            forecasts_test.append(thisforecast)

            last_train_day = np.array(scaled_df.iloc[-1][0]).reshape(1, -1)

            unscaled_first_test_day = (
                int(scalers_dict[SIM_DATE_NAME].inverse_transform(last_train_day)) + 1
            )

            predicted_days = np.arange(
                unscaled_first_test_day, unscaled_first_test_day + PREDICT_DAYS
            )
            dates_test.append(predicted_days)

        logging.info("about to plot")
        LINEWIDTH = 1
        # plot training predictions
        for n in range(len(dates_train)):
            i = dates_train[n]
            j = np.squeeze(forecasts_train[n])
            # newdates = convert_to_2020_date(i,args)
            newdates = dates_train[n]
            logging.info(i)
            logging.info(j)
            logging.info(newdates)
            logging.info("got inputs for plotting")
            if n == 0:
                plt.plot(
                    newdates, j, color="blue", label="Train Set", linewidth=LINEWIDTH, markersize=0
                )
            else:
                plt.plot(newdates, j, color="blue", linewidth=LINEWIDTH, markersize=0)
            # check if dictionary of scalers works
            logging.info("plotted one")

        for n in range(len(dates_test)):
            i = dates_test[n]
            j = np.squeeze(forecasts_test[n])
            # newdates = convert_to_2020_date(i,args)
            newdates = dates_test[n]
            logging.info(i)
            logging.info(j)
            logging.info(newdates)
            logging.info("got inputs for plotting")
            if n == 0:
                plt.plot(
                    newdates, j, color="orange", label="Test Set", linewidth=LINEWIDTH, markersize=0
                )
            else:
                plt.plot(newdates, j, color="orange", linewidth=LINEWIDTH, markersize=0)
            # check if dictionary of scalers works
            logging.info("plotted one")

        # full_data = test_df_list[-1]
        full_data = df_forecast
        plt.plot(
            full_data[SIM_DATE_NAME],
            full_data[PREDICT_VARIABLE],
            linewidth=LINEWIDTH,
            markersize=1,
            label="Data",
        )
        plt.xlabel(SIM_DATE_NAME)
        plt.ylabel(PREDICT_VARIABLE)
        plt.legend()

        plt.savefig("train_plot.pdf")

        return

    @staticmethod
    def build_model(
        MASK_VALUE, epochs, n_batch, hidden_layer_dimensions, dropout, final_train_X, final_train_Y
    ):
        patience = 50
        validation_split = 0.1
        model = Sequential()
        model.add(
            Masking(
                mask_value=MASK_VALUE,
                batch_input_shape=(n_batch, final_train_X.shape[1], final_train_X.shape[2]),
            )
        )
        model.add(
            LSTM(
                hidden_layer_dimensions,
                batch_input_shape=(n_batch, final_train_X.shape[1], final_train_X.shape[2]),
                stateful=True,
                return_sequences=True,
            )
        )
        model.add(
            LSTM(
                hidden_layer_dimensions,
                batch_input_shape=(n_batch, final_train_X.shape[1], final_train_X.shape[2]),
                stateful=True,
            )
        )
        model.add(Dropout(dropout))
        model.add(Dense(final_train_Y.shape[1]))
        es = EarlyStopping(monitor="val_loss", mode="min", verbose=1, patience=patience)
        model.compile(loss="mean_squared_error", optimizer="adam")
        history = model.fit(
            final_train_X,
            final_train_Y,
            epochs=epochs,
            batch_size=n_batch,
            verbose=1,
            shuffle=False,
            validation_split=validation_split,
            callbacks=[es],
        )
        logging.info("fit")
        logging.info(history.history["loss"])
        logging.info(history.history["val_loss"])
        plot = True
        if plot:
            plt.close("all")
            logging.info("plotting")
            plt.plot(history.history["loss"], color="blue", linestyle="solid", label="Train Set")
            logging.info("plotted history")
            plt.plot(
                history.history["val_loss"],
                color="green",
                linestyle="solid",
                label="Validation Set",
            )
            logging.info("plotted more")
            plt.legend()
            plt.xlabel("Epochs")
            plt.ylabel("RMSE")
            plt.savefig("lstm_loss_final.png")
            plt.close("all")

        return model, history

    @staticmethod
    def get_X_Y(sample_list, PREDICT_DAYS, PREDICT_VARIABLE, MASK_VALUE):
        PREDICT_VAR = PREDICT_VARIABLE + "_scaled"
        SEQUENCE_LENGTH = 300
        X_train_list = list()
        Y_train_list = list()
        df_list = list()
        log.info("SAMPLE LIST LENGTH")
        log.info(len(sample_list))
        for i in range(len(sample_list)):
            df = sample_list[i]
            df_list.append(df)
            df = df.filter(regex="scaled")

            train = df.iloc[:-PREDICT_DAYS, :]  # exclude last n entries of df to use for prediction
            test = df.iloc[-PREDICT_DAYS:, :]

            n_rows_train = train.shape[0]
            n_rows_to_add = SEQUENCE_LENGTH - n_rows_train
            pad_rows = np.empty((n_rows_to_add, train.shape[1]), float)
            pad_rows[:] = MASK_VALUE
            padded_train = np.concatenate((pad_rows, train))

            test = np.array(test[PREDICT_VAR])

            X_train_list.append(padded_train)
            Y_train_list.append(test)

        final_test_X = np.array(X_train_list)
        final_test_Y = np.array(Y_train_list)
        final_test_Y = np.squeeze(final_test_Y)
        return final_test_X, final_test_Y, df_list

    @staticmethod
    def create_df_list(df, min_days, predict_days):
        df_list = list()
        for i in range(len(df.index)):
            if i < predict_days + min_days:  # only keep df if it has min number of entries
                continue
            else:
                if self.SAMPLE_LENGTH == -1:  # use all historical data for every sample
                    df_list.append(df[:i].copy())
                else:  # use only SAMPLE_LENGTH historical days of data
                    df_list.append(df[i - self.SAMPLE_LENGTH : i].copy())
        return df_list

    @staticmethod
    def get_reshaped_X(input_X, n_batch, X_scaler):
        i = input_X.reshape(n_batch, input_X.shape[0], input_X.shape[1])
        output_df = pd.DataFrame(np.squeeze(input_X))
        # original_df = pd.DataFrame(X_scaler.inverse_transform(output_df))
        return original_df