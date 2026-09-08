# -*- encoding: utf-8 -*-

import multiprocessing
import time
from ..indicators import pd, np
from itertools import product
from random import random, choice
from psutil import cpu_count
from deap import creator, base, tools, algorithms


class ParamInfo:
    '''
    参数信息类
    '''

    def __init__(self, name: str, start_val=None, end_val=None, step_val=None, ndigits=1, val_list: list = None) -> None:
        self.name = name  # 参数名
        self.start_val = start_val  # 起始值
        self.end_val = end_val  # 结束值
        self.step_val = step_val  # 变化步长
        self.ndigits = ndigits  # 小数位
        self.val_list = val_list  # 指定参数

    def gen_array(self) -> list:
        if self.val_list is not None:
            return self.val_list

        values = list()
        curVal = round(self.start_val, self.ndigits)
        while curVal < self.end_val:
            values.append(curVal)

            curVal += self.step_val
            curVal = round(curVal, self.ndigits)
            if curVal >= self.end_val:
                curVal = self.end_val
                break
        values.append(round(curVal, self.ndigits))
        return values


class GAOptimizer:
    '''
    参数优化器\n
    主要用于做策略参数优化的
    '''

    def __init__(self, strategy, datas, target, worker_num: int = None, MU: int = 80, population_size: int = 100, ngen_size: int = 20,
                 cx_prb: float = 0.9, isstats=False, show_bar=False):
        '''
        构造函数\n

        @worker_num 工作进程个数，默认为2，可以根据CPU核心数设置，由于计算回测值是从文件里读取，因此进程过多可能会出现冲突\n
        @MU        每一代选择的个体数\n
        @population_size 种群数\n
        @ngen_size 进化代数\n
        @cx_prb    交叉概率\n
        @mut_prb   变异概率
        '''
        self.worker_num = worker_num if worker_num and worker_num > 0 else cpu_count()-1
        self.running_worker = 0
        self.mutable_params: dict[str, ParamInfo] = dict()
        self.strategy = strategy
        self.strategy_name = strategy.__name__
        self.optimizing_target = target if isinstance(
            target, (list, tuple)) else [target,]
        self.optimizing_num = tuple(
            0. for _ in range(len(self.optimizing_target)))
        self.datas = datas
        self.isstats = isstats
        self.show_bar = show_bar

        self.population_size = population_size  # 种群数
        self.ngen_size = ngen_size  # 进化代数，即优化迭代次数，根据population_size大小设定
        self.MU = MU  # 每一代选择的个体数，可以取个体数的0.8倍
        self.lambda_ = self.population_size  # 下一代产生的个体数
        self.cx_prb = cx_prb  # 建议取0.4~0.99之间
        self.mut_prb = 1-cx_prb  # 建议取0.0001~0.1之间

        self.cache_dict = multiprocessing.Manager().dict()  # 缓存中间结果
        # self.__isNAN:bool=True

    def add_mutable_param(self, name: str, start_val, end_val, step_val, ndigits=1):
        '''
        添加可变参数\n

        @name       参数名\n
        @start_val  起始值\n
        @end_val    结束值\n
        @step_val   步长\n
        @ndigits    小数位
        '''
        self.mutable_params[name] = ParamInfo(name=name, start_val=start_val, end_val=end_val, step_val=step_val,
                                              ndigits=ndigits)

    def add_listed_param(self, name: str, val_list: list):
        '''
        添加限定范围的可变参数\n

        @name       参数名\n
        @val_list   参数值列表
        '''
        self.mutable_params[name] = ParamInfo(name=name, val_list=val_list)

    def add_fixed_param(self, name: str, val):
        '''
        添加固定参数\n

        @name       参数名\n
        @val        值\n
        '''
        self.mutable_params[name] = ParamInfo(name=name, val_list=[val,])

    def generate_settings(self):
        ''' 生成优化参数组合 '''
        # 参数名列表
        name_list = self.mutable_params.keys()

        param_list = []
        for name in name_list:
            paraminfo = self.mutable_params[name]
            values = paraminfo.gen_array()
            param_list.append(values)

        # 使用迭代工具产生参数对组合
        products = list(product(*param_list))

        # 把参数对组合打包到字典列表里
        settings = []
        [settings.append(dict(zip(name_list, p))) for p in products]
        return settings

    def mututate_individual(self, individual, indpb):
        """
        变异函数
        :param individual: 个体，实际为策略参数
        :param indpb: 变异概率
        :return: 变异后的个体
        """
        size = len(individual)
        param_list = self.generate_settings()
        settings = [list(item.items()) for item in param_list]

        for i in range(size):
            if random() < indpb:
                individual[i] = settings[i]
        return individual,

    def __run(self, t):
        t._processing_data(self.datas)
        t = t()
        t._data_init_()
        t.start()

        def core(x):
            t._pre_next(x)
            t.next()
            t.index += 1
            t._update_history()
        list(map(core, t._get_rolling_datas()))
        t.stop()
        t._get_result()
        t._qs_init()
        stats = t._stats
        results = []
        for target in self.optimizing_target:
            result = getattr(stats, target)()
            result = result if isinstance(result, float) else list(result)[-1]
            # result=result,
            results.append(result)
        return tuple(results)

    def evaluate_func(self, params):
        """
        适应度函数
        :return:
        """
        if any([isinstance(x, list) for x in params]):
            params = params[0]

        if len(params) < 1:
            print(f"Empty parameters: {params}")
            return self.optimizing_num

        if isinstance(params, tuple):
            return self.optimizing_num

        if not all([isinstance(x, tuple) for x in params]):
            return self.optimizing_num

        params = dict(params)

        # strategy name
        strategy_name = "".join(
            [self.strategy_name, *[f"_{k}_{v}" for k, v in params.items()]])
        t = type(strategy_name, (self.strategy,), dict(params=params))
        results = self.__run(t)
        self.cache_dict[strategy_name] = results
        return results

    def run_ga_optimizer(self):
        """ 执行GA优化 """
        # 遗传算法参数空间
        buffer = self.generate_settings()
        settings = [list(itm.items()) for itm in buffer]

        def generate_parameter():
            return choice(settings)

        pool = multiprocessing.Pool(self.worker_num)  # 多线程设置
        toolbox = base.Toolbox()
        toolbox.register("individual", tools.initIterate,
                         creator.Individual, generate_parameter)
        toolbox.register("population", tools.initRepeat,
                         list, toolbox.individual)
        toolbox.register("mate", tools.cxTwoPoint)
        # toolbox.register("mutate", tools.mutUniformInt,low = 4,up = 40,indpb=0.6)
        # toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=1, indpb=0.1)
        toolbox.register("mutate", self.mututate_individual,
                         indpb=self.mut_prb)
        # indpb=0.05)  # 0.05)
        toolbox.register("evaluate", self.evaluate_func)
        toolbox.register("select", tools.selNSGA2)
        toolbox.register("map", pool.map)  # 多线程优化，可能会报错
        # seed(12555888)  # 固定随机数种子

        pop = toolbox.population(self.population_size)
        hof = tools.ParetoFront()  # 非占优最优集
        # hof = tools.HallOfFame(1)
        stats = tools.Statistics(lambda ind: ind.fitness.values)
        np.set_printoptions(suppress=True)
        if self.isstats:
            stats.register("avg", np.mean, axis=0)
            stats.register("std", np.std, axis=0)
            stats.register("min", np.min, axis=0)
            stats.register("max", np.max, axis=0)

        # Run ga optimization
        print("*" * 50)
        print(f"开始执行遗传算法优化...")
        print(f"参数优化空间: {len(settings)}")
        print(f"每代族群总数: {self.population_size}")
        print(f"优良个体筛选数: {self.MU}")
        print(f"迭代次数: {self.ngen_size}")
        print(f"交叉几率: {self.cx_prb:.2%}")
        print(f"变异几率: {self.mut_prb:.2%}")

        begin = time.perf_counter()
        _, logbook = eaMuPlusLambda(pop, toolbox, self.MU, self.lambda_, self.cx_prb, self.mut_prb,
                                    self.ngen_size, stats, verbose=False, halloffame=hof, show_bar=self.show_bar)

        end = time.perf_counter()
        print(f"算法优化完成，耗时: {end - begin: .2f} 秒")
        print("*" * 50)

        # 处理结果
        # optimizing_value = [item['max'][0] for item in logbook]
        # optimizing_params = [{item[0]: item[1]} for item in hof[0]]
        # optimizing_params.append({f"{self.optimizing_target}": max(optimizing_value)})
        # print(optimizing_params)
        # return optimizing_params

    def go(self, weights, dir='./minibt/op_params/'):
        '''
        启动优化器\n
        @markerfile 标记文件名，回测完成以后分析会用到
        '''
        self.run_ga_optimizer()

        # 获取所有的值
        # list(self.cache_dict.values())
        results = [[k, *v] for k, v in self.cache_dict.items()]
        header = ['params', *self.optimizing_target]
        # data = [list(itm.values()) for itm in results]
        df_summary = pd.DataFrame(results, columns=header)
        # df_summary = df_results[["name", self.optimizing_target]]
        if len(weights) > 1:
            for i, w in enumerate(weights):
                df = df_summary.copy()
                df.sort_values(
                    by=self.optimizing_target[i], ascending=w < 0., inplace=True)
                df.reset_index(inplace=True, drop=True)
                out_summary_file = "_".join(
                    [self.strategy_name, self.optimizing_target[i]])
                df.to_csv(f"{dir}{out_summary_file}.csv",
                          encoding='utf-8-sig', index=False)
                print(
                    f'优化目标: {self.optimizing_target[i]}, 优化最大值：{df.iloc[0].tolist()}')

        df_summary.sort_values(by=self.optimizing_target,
                               ascending=weights[0] < 0., inplace=True)
        df_summary.reset_index(inplace=True, drop=True)
        out_summary_file = self.strategy_name
        df_summary.to_csv(f"{dir}{out_summary_file}.csv",
                          encoding='utf-8-sig', index=False)
        print(
            f'优化目标: {self.optimizing_target}, 优化最大值：{df_summary.iloc[0].tolist()}')


def eaMuPlusLambda(population, toolbox, mu, lambda_, cxpb, mutpb, ngen,
                   stats=None, halloffame=None, verbose=__debug__, show_bar=False):
    r"""This is the :math:`(\mu + \lambda)` evolutionary algorithm.

    :param population: A list of individuals.
    :param toolbox: A :class:`~deap.base.Toolbox` that contains the evolution
                    operators.
    :param mu: The number of individuals to select for the next generation.
    :param lambda\_: The number of children to produce at each generation.
    :param cxpb: The probability that an offspring is produced by crossover.
    :param mutpb: The probability that an offspring is produced by mutation.
    :param ngen: The number of generation.
    :param stats: A :class:`~deap.tools.Statistics` object that is updated
                  inplace, optional.
    :param halloffame: A :class:`~deap.tools.HallOfFame` object that will
                       contain the best individuals, optional.
    :param verbose: Whether or not to log the statistics.
    :returns: The final population
    :returns: A class:`~deap.tools.Logbook` with the statistics of the
              evolution.

    The algorithm takes in a population and evolves it in place using the
    :func:`varOr` function. It returns the optimized population and a
    :class:`~deap.tools.Logbook` with the statistics of the evolution. The
    logbook will contain the generation number, the number of evaluations for
    each generation and the statistics if a :class:`~deap.tools.Statistics` is
    given as argument. The *cxpb* and *mutpb* arguments are passed to the
    :func:`varOr` function. The pseudocode goes as follow ::

        evaluate(population)
        for g in range(ngen):
            offspring = varOr(population, toolbox, lambda_, cxpb, mutpb)
            evaluate(offspring)
            population = select(population + offspring, mu)

    First, the individuals having an invalid fitness are evaluated. Second,
    the evolutionary loop begins by producing *lambda_* offspring from the
    population, the offspring are generated by the :func:`varOr` function. The
    offspring are then evaluated and the next generation population is
    selected from both the offspring **and** the population. Finally, when
    *ngen* generations are done, the algorithm returns a tuple with the final
    population and a :class:`~deap.tools.Logbook` of the evolution.

    This function expects :meth:`toolbox.mate`, :meth:`toolbox.mutate`,
    :meth:`toolbox.select` and :meth:`toolbox.evaluate` aliases to be
    registered in the toolbox. This algorithm uses the :func:`varOr`
    variation.
    """
    logbook = tools.Logbook()
    logbook.header = ['gen', 'nevals'] + (stats.fields if stats else [])

    # Evaluate the individuals with an invalid fitness
    invalid_ind = [ind for ind in population if not ind.fitness.valid]
    fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)
    for ind, fit in zip(invalid_ind, fitnesses):
        ind.fitness.values = fit

    if halloffame is not None:
        halloffame.update(population)

    record = stats.compile(population) if stats is not None else {}
    logbook.record(gen=0, nevals=len(invalid_ind), **record)
    if verbose:
        print(logbook.stream)

    def go(gen):
        # for gen in range(1, ngen + 1):
        # Vary the population
        # for gen in pbar:
        offspring = algorithms.varOr(population, toolbox, lambda_, cxpb, mutpb)

        # Evaluate the individuals with an invalid fitness
        invalid_ind = [ind for ind in offspring if not ind.fitness.valid]
        fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)
        for ind, fit in zip(invalid_ind, fitnesses):
            ind.fitness.values = fit

        # Update the hall of fame with the generated individuals
        if halloffame is not None:
            halloffame.update(offspring)

        # Select the next generation population
        population[:] = toolbox.select(population + offspring, mu)

        # Update the statistics with the new population
        record = stats.compile(population) if stats is not None else {}
        logbook.record(gen=gen, nevals=len(invalid_ind), **record)
        if verbose:
            print(logbook.stream)
    # Begin the generational process
    show = True
    try:
        from tqdm import tqdm
    except:
        print('tqdm模块未安装')
        show = False
    if show_bar and show:
        with tqdm(range(1, ngen + 1), colour='red', leave=True, position=0, ncols=160) as pbar:
            for gen in pbar:
                go(gen)
    else:
        for gen in range(1, ngen + 1):
            go(gen)

    return population, logbook
