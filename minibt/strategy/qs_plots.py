from ..utils import qs_plots, pd


class QSPlots(pd.Series):

    def __init__(self, data=None, index=None, dtype=None, name=None, copy=False) -> None:
        super().__init__(data, index, dtype, name, copy)

    @staticmethod
    @qs_plots
    def to_plotly(fig):
        ...

    @qs_plots
    def snapshot(self, grayscale=False, figsize=(10, 8),
                 title='Portfolio Summary', fontname='Arial', lw=1.5,
                 mode="comp", subtitle=True, savefig=None, show=True,
                 log_scale=False):

        ...

    @qs_plots
    def earnings(self, start_balance=1e5, mode="comp",
                 grayscale=False, figsize=(10, 6),
                 title='Portfolio Earnings',
                 fontname='Arial', lw=1.5,
                 subtitle=True, savefig=None, show=True):

        ...

    @qs_plots
    def returns(self, benchmark=None,
                grayscale=False, figsize=(10, 6),
                fontname='Arial', lw=1.5,
                match_volatility=False, compound=True, cumulative=True,
                resample=None, ylabel="Cumulative Returns",
                subtitle=True, savefig=None, show=True,
                prepare_returns=True):

        ...

    @qs_plots
    def log_returns(self, benchmark=None,
                    grayscale=False, figsize=(10, 5),
                    fontname='Arial', lw=1.5,
                    match_volatility=False, compound=True, cumulative=True,
                    resample=None, ylabel="Cumulative Returns",
                    subtitle=True, savefig=None, show=True,
                    prepare_returns=True):

        ...

    @qs_plots
    def daily_returns(self,
                      grayscale=False, figsize=(10, 4),
                      fontname='Arial', lw=0.5,
                      log_scale=False, ylabel="Returns",
                      subtitle=True, savefig=None, show=True,
                      prepare_returns=True):

        ...

    @qs_plots
    def yearly_returns(self, benchmark=None,
                       fontname='Arial', grayscale=False,
                       hlw=1.5, hlcolor="red", hllabel="",
                       match_volatility=False,
                       log_scale=False, figsize=(10, 5), ylabel=True,
                       subtitle=True, compounded=True,
                       savefig=None, show=True,
                       prepare_returns=True):

        ...

    @qs_plots
    def distribution(self, fontname='Arial', grayscale=False, ylabel=True,
                     figsize=(10, 6), subtitle=True, compounded=True,
                     savefig=None, show=True,
                     prepare_returns=True):
        ...

    @qs_plots
    def histogram(self, resample='M', fontname='Arial',
                  grayscale=False, figsize=(10, 5), ylabel=True,
                  subtitle=True, compounded=True, savefig=None, show=True,
                  prepare_returns=True):

        ...

    @qs_plots
    def drawdown(self, grayscale=False, figsize=(10, 5),
                 fontname='Arial', lw=1, log_scale=False,
                 match_volatility=False, compound=False, ylabel="Drawdown",
                 resample=None, subtitle=True, savefig=None, show=True):

        ...

    @qs_plots
    def drawdowns_periods(self, periods=5, lw=1.5, log_scale=False,
                          fontname='Arial', grayscale=False, figsize=(10, 5),
                          ylabel=True, subtitle=True, compounded=True,
                          savefig=None, show=True,
                          prepare_returns=True):
        ...

    @qs_plots
    def rolling_beta(self, benchmark,
                     window1=126, window1_label="6-Months",
                     window2=252, window2_label="12-Months",
                     lw=1.5, fontname='Arial', grayscale=False,
                     figsize=(10, 3), ylabel=True,
                     subtitle=True, savefig=None, show=True,
                     prepare_returns=True):

        ...

    @qs_plots
    def rolling_volatility(self, benchmark=None,
                           period=126, period_label="6-Months",
                           periods_per_year=252,
                           lw=1.5, fontname='Arial', grayscale=False,
                           figsize=(10, 3), ylabel="Volatility",
                           subtitle=True, savefig=None, show=True):

        ...

    @qs_plots
    def rolling_sharpe(self, benchmark=None, rf=0.,
                       period=126, period_label="6-Months",
                       periods_per_year=252,
                       lw=1.25, fontname='Arial', grayscale=False,
                       figsize=(10, 3), ylabel="Sharpe",
                       subtitle=True, savefig=None, show=True):

        ...

    @qs_plots
    def rolling_sortino(self, benchmark=None, rf=0.,
                        period=126, period_label="6-Months",
                        periods_per_year=252,
                        lw=1.25, fontname='Arial', grayscale=False,
                        figsize=(10, 3), ylabel="Sortino",
                        subtitle=True, savefig=None, show=True):

        ...

    @qs_plots
    def monthly_heatmap(self, annot_size=10, figsize=(10, 5),
                        cbar=True, square=False,
                        compounded=True, eoy=False,
                        grayscale=False, fontname='Arial',
                        ylabel=True, savefig=None, show=True):

        ...

    @qs_plots
    def monthly_returns(self, annot_size=10, figsize=(10, 5),
                        cbar=True, square=False,
                        compounded=True, eoy=False,
                        grayscale=False, fontname='Arial',
                        ylabel=True, savefig=None, show=True):
        ...
