import pandas as pd
import pyModeS as pms
from typing import Iterable
from pyms4os.opensky_impala_wrapper import OpenskyImpalaWrapper


class MeteoHelper(object):
    """docstring for MeteoHelper."""

    def __init__(self):
        super(MeteoHelper, self).__init__()
        self.opensky = OpenskyImpalaWrapper()

    def get(self, icao24, start, end, include45=False):
        df = self.opensky.query(
            type="raw", start=start, end=end, icao24=icao24
        )

        if df is None:
            return

        print("**Processing data...")

        df = df.sort_values("mintime")
        df["DF"] = df["rawmsg"].apply(pms.df)

        commb = df[df["DF"].isin([20, 21])][
            ["icao24", "mintime", "rawmsg", "DF"]
        ]

        commb.loc[commb.DF == 20, "altitude"] = commb.loc[
            commb.DF == 20, "rawmsg"
        ].apply(pms.altcode)

        commb.loc[commb.DF == 21, "squawk"] = commb.loc[
            commb.DF == 21, "rawmsg"
        ].apply(pms.idcode)

        commb["bds"] = commb["rawmsg"].apply(pms.bds.infer, args=[True])

        if include45:
            bds_codes = ["BDS44", "BDS45"]
        else:
            bds_codes = ["BDS44"]

        ehs = commb[commb["bds"].isin(bds_codes)]

        # construct colums of the data frame based on BDS44, BDS45
        columns = ["icao24", "time", "rawmsg", "bds", "altitude", "squawk"]

        columns.extend(
            ["wind44spd", "wind44dir", "temp44", "p44", "hum44", "turb44"]
        )

        if include45:
            columns.extend(
                [
                    "turb45",
                    "ws45",
                    "mb45",
                    "ic45",
                    "wv45",
                    "temp45",
                    "p45",
                    "rh45",
                ]
            )
        dfout = pd.DataFrame(columns=columns)

        # decode messages row by row
        for i, r in ehs.iterrows():
            bds = r["bds"]
            msg = r["rawmsg"]

            d = {
                "time": r["mintime"],
                "icao24": r["icao24"],
                "bds": bds,
                "rawmsg": r["rawmsg"],
                "altitude": r["altitude"],
                "squawk": r["squawk"],
            }

            if bds == "BDS44":
                wind44spd, wind44dir = pms.commb.wind44(msg)
                temp44, _ = pms.commb.temp44(msg)

                p44 = pms.commb.p44(msg)
                hum44 = pms.commb.hum44(msg)
                turb44 = pms.commb.turb44(msg)

                d.update(
                    {
                        "wind44spd": wind44spd,
                        "wind44dir": wind44dir,
                        "temp44": temp44,
                        "p44": p44,
                        "hum44": hum44,
                        "turb44": turb44,
                    }
                )

            if bds == "BDS45":
                turb45 = pms.commb.turb45(msg)
                ws45 = pms.commb.ws45(msg)
                mb45 = pms.commb.mb45(msg)
                ic45 = pms.commb.ic45(msg)
                wv45 = pms.commb.wv45(msg)
                temp45 = pms.commb.temp45(msg)
                p45 = pms.commb.p45(msg)
                rh45 = pms.commb.rh45(msg)

                d.update(
                    {
                        "turb45": turb45,
                        "ws45": ws45,
                        "mb45": mb45,
                        "ic45": ic45,
                        "wv45": wv45,
                        "temp45": temp45,
                        "p45": p45,
                        "rh45": rh45,
                    }
                )

            dfout = dfout.append(d, ignore_index=True)
        return dfout
