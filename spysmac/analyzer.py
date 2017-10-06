import os
import logging
from collections import OrderedDict
import typing
import json

import numpy as np
from pandas import DataFrame

from smac.configspace import Configuration
from smac.epm.rf_with_instances import RandomForestWithInstances
from smac.optimizer.objective import average_cost
from smac.runhistory.runhistory import RunKey, RunValue, RunHistory
from smac.runhistory.runhistory2epm import RunHistory2EPM4Cost
from smac.scenario.scenario import Scenario
from smac.utils.io.traj_logging import TrajLogger
from smac.utils.validate import Validator

from pimp.importance.importance import Importance

from spysmac.html.html_builder import HTMLBuilder
from spysmac.plot.plotter import Plotter
from spysmac.smacrun import SMACrun
from spysmac.utils.helpers import get_cost_dict_for_config

__author__ = "Joshua Marben"
__copyright__ = "Copyright 2017, ML4AAD"
__license__ = "3-clause BSD"
__maintainer__ = "Joshua Marben"
__email__ = "joshua.marben@neptun.uni-freiburg.de"

class Analyzer(object):
    """
    Analyze SMAC-output data.
    Compares two configurations (default vs incumbent) over multiple SMAC-runs
    and outputs PAR10, timeouts, scatterplots, parameter importance etc.
    """

    def __init__(self, global_rh, train_test, scenario):
        """Saves all relevant information that arises during the analysis. """
        self.logger = logging.getLogger("spysmac.analyzer")

        self.global_rh = global_rh
        self.train_test = train_test
        self.scenario = scenario

        self.performance_table = None

    def get_timeouts(self, config):
        """ Get number of timeouts in config per runs in total (not per
        instance) """
        costs = get_cost_dict_for_config(self.global_rh, config, metric='time')
        cutoff = self.scenario.cutoff
        if self.train_test:
            if not cutoff:
                return (("N","A"),("N","A"))
            train_timeout, test_timeout = 0, 0
            train_no_timeout, test_no_timeout = 0, 0
            for run in costs:
                if (cutoff and run.instance in self.scenario.train_insts and
                        costs[run] >= cutoff):
                    train_timeout += 1
                elif (cutoff and run.instance in self.scenario.train_insts and
                        costs[run] < cutoff):
                    train_no_timeout += 1
                if (cutoff and run.instance in self.scenario.test_insts and
                        costs[run] >= cutoff):
                    test_timeout += 1
                elif (cutoff and run.instance in self.scenario.test_insts and
                        costs[run] < cutoff):
                    test_no_timeout += 1
            return ((train_timeout, train_no_timeout), (test_timeout, test_no_timeout))
        else:
            if not cutoff:
                return ("N","A")
            timeout, no_timeout = 0, 0
            for run in costs:
                if cutoff and costs[run] >= cutoff:
                    timeout += 1
                else:
                    no_timeout += 1
            return (timeout, no_timeout)

    def get_parX(self, config, par=10):
        """Calculate parX-values of default and incumbent configs.
        First determine PAR-timeouts for each run on each instances,
        Second average over train/test if available, else just average.

        Parameters
        ----------
        config: Configuration
            config to be calculated
        par: int
            par-factor to use

        Returns
        -------
        (train, test) OR average -- tuple<float, float> OR float
            PAR10 values for train- and test-instances, if available as tuple
            else the general average
        """
        runs = get_cost_dict_for_config(self.global_rh, config)
        # Penalize
        if self.scenario.cutoff:
            runs = [(k.instance, runs[k]) if runs[k] < self.scenario.cutoff
                        else (k.instance, self.scenario.cutoff*par)
                        for k in runs]
        else:
            runs = [(k.instance, runs[k]) for k in runs]
            self.logger.info("Calculating penalized average runtime without "
                             "cutoff...")

        # Average
        if self.train_test:
            train = np.mean([c for i, c in runs if i in
                             self.scenario.train_insts])
            test = np.mean([c for i, c in runs if i in
                            self.scenario.test_insts])
            return (train, test)
        else:
            return np.mean([c for i, c in runs])

    def parameter_importance(self, modus, incumbent, output):
        """Calculate parameter-importance using the PIMP-package.
        Currently ablation, forward-selection and fanova are used.

        Parameters
        ----------
        modus: str
            modus for parameter importance, from [forward-selection, ablation,
            fanova]
        """
        # Get parameter-values and costs per config in arrays.
        configs = self.global_rh.get_all_configs()
        X = np.array([c.get_array() for c in configs])
        y = np.array([self.global_rh.get_cost(c) for c in configs])

        # Evaluate parameter importance
        save_folder = output
        importance = Importance(scenario=self.scenario,
                                runhistory=self.global_rh,
                                incumbent=incumbent,
                                parameters_to_evaluate=len(self.scenario.cs.get_hyperparameters()),
                                save_folder=save_folder,
                                seed=12345)
        result = importance.evaluate_scenario(modus)
        with open(os.path.join(save_folder, 'pimp_values_%s.json' % modus), 'w') as out_file:
            json.dump(result, out_file, sort_keys=True, indent=4, separators=(',', ': '))
        importance.plot_results(name=os.path.join(save_folder, modus), show=False)

    def create_overview_table(self, best_folder):
        """ Create overview-table. """
        overview = OrderedDict([('Run with best incumbent', best_folder),
                                ('# Train instances', len(self.scenario.train_insts)),
                                ('# Test instances', len(self.scenario.test_insts)),
                                ('# Parameters', len(self.scenario.cs.get_hyperparameters())),
                                ('Cutoff', self.scenario.cutoff),
                                ('Walltime budget', self.scenario.wallclock_limit),
                                ('Runcount budget', self.scenario.ta_run_limit),
                                ('CPU budget', self.scenario.algo_runs_timelimit),
                                ('Deterministic', self.scenario.deterministic),
                               ])
        # Split into two columns
        overview_split = self._split_table(overview)
        # Convert to HTML
        df = DataFrame(data=overview_split)
        table = df.to_html(escape=False, header=False, index=False, justify='left')
        return table

    def create_performance_table(self, default, incumbent):
        """Create table, compare default against incumbent on train-,
        test- and combined instances. Listing PAR10, PAR1 and timeouts.
        Distinguishes between train and test, if available."""
        def_timeout, inc_timeout = self.get_timeouts(default), self.get_timeouts(incumbent)
        def_par10, inc_par10 = self.get_parX(default, 10), self.get_parX(incumbent, 10)
        def_par1, inc_par1 = self.get_parX(default, 1), self.get_parX(incumbent, 1)
        dec_place = 3
        if self.train_test:
            # Distinction between train and test
            # Create table
            array = np.array([[round(def_par10[0], dec_place),
                               round(def_par10[1], dec_place),
                               round(inc_par10[0], dec_place),
                               round(inc_par10[1], dec_place)],
                              [round(def_par1[0], dec_place),
                               round(def_par1[1], dec_place),
                               round(inc_par1[0], dec_place),
                               round(inc_par1[1], dec_place)],
                              ["{}/{}".format(def_timeout[0][0], def_timeout[0][1]),
                               "{}/{}".format(def_timeout[1][0], def_timeout[1][1]),
                               "{}/{}".format(inc_timeout[0][0], inc_timeout[0][1]),
                               "{}/{}".format(inc_timeout[1][0], inc_timeout[1][1])
                               ]])
            df = DataFrame(data=array, index=['PAR10', 'PAR1', 'Timeouts'],
                           columns=['Train', 'Test', 'Train', 'Test'])
            table = df.to_html()
            # Insert two-column-header
            table = table.split(sep='</thead>', maxsplit=1)[1]
            new_table = "<table border=\"3\" class=\"dataframe\">\n"\
                        "  <col>\n"\
                        "  <colgroup span=\"2\"></colgroup>\n"\
                        "  <colgroup span=\"2\"></colgroup>\n"\
                        "  <thead>\n"\
                        "    <tr>\n"\
                        "      <td rowspan=\"2\"></td>\n"\
                        "      <th colspan=\"2\" scope=\"colgroup\">Default</th>\n"\
                        "      <th colspan=\"2\" scope=\"colgroup\">Incumbent</th>\n"\
                        "    </tr>\n"\
                        "    <tr>\n"\
                        "      <th scope=\"col\">Train</th>\n"\
                        "      <th scope=\"col\">Test</th>\n"\
                        "      <th scope=\"col\">Train</th>\n"\
                        "      <th scope=\"col\">Test</th>\n"\
                        "    </tr>\n"\
                        "</thead>\n"
            table = new_table + table
        else:
            # No distinction between train and test
            array = np.array([[round(def_par10, dec_place),
                               round(inc_par10, dec_place)],
                              [round(def_par1, dec_place),
                               round(inc_par1, dec_place)],
                              ["{}/{}".format(def_timeout[0], def_timeout[1]),
                               "{}/{}".format(inc_timeout[0], inc_timeout[1])]])
            df = DataFrame(data=array, index=['PAR10', 'PAR1', 'Timeouts'],
                           columns=['Default', 'Incumbent'])
            table = df.to_html()
        self.performance_table = table
        return table

    def config_to_html(self, default: Configuration, incumbent: Configuration):
        """Create HTML-table to compare Configurations.
        Removes unused parameters.

        Parameters
        ----------
        default, incumbent: Configurations
            configurations to be converted

        Returns
        -------
        table: str
            HTML-table comparing default and incumbent
        """
        # Remove unused parameters
        keys = [k for k in default.keys() if default[k] or incumbent[k]]
        default = [default[k] for k in keys]
        incumbent = [incumbent[k] for k in keys]
        table = list(zip(default, incumbent))
        df = DataFrame(data=table, columns=["Default", "Incumbent"], index=keys)
        table = df.to_html()
        return table

    def _split_table(self, table: OrderedDict):
        """Splits an OrderedDict into a list of tuples that can be turned into a
        HTML-table with pandas DataFrame

        Parameters
        ----------
        table: OrderedDict
            table that is to be split into two columns

        Returns
        -------
        table_split: List[tuple(key, value, key, value)]
            list with two key-value pairs per entry that can be used by pandas
            df.to_html()
        """
        table_split = []
        keys = list(table.keys())
        half_size = len(keys)//2
        for i in range(half_size):
            j = i + half_size
            table_split.append(("<b>"+keys[i]+"</b>", table[keys[i]],
                                "<b>"+keys[j]+"</b>", table[keys[j]]))
        if len(keys)%2 == 1:
            table_split.append(("<b>"+keys[-1]+"</b>", table[keys[-1]], '', ''))
        return table_split

