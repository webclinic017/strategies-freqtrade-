#!/bin/zsh

# runs a single strategy

clean=0
epochs=100
jarg=""
config_file=""

spaces="buy sell"

#get date from 30 days ago (MacOS-specific)
num_days=180
start_date=$(date -j -v-${num_days}d +"%Y%m%d")
timerange="${start_date}-"

show_usage () {
    script=$(basename $BASH_SOURCE)
    cat << END

Usage: zsh $script [options] <exchange> <strategy>

[options]:  -c | --config      path to config file (default: user_data/strategies/<exchange>/config_<exchange>.json
            -n | --ndays       Number of days of backtesting. Defaults to ${ndays}
            -t | --timeframe   Timeframe (YYYMMDD-[YYYMMDD]). Defaults to last ${ndays} days (${timerange})

<exchange>  Name of exchange (binanceus, coinbasepro, kucoin, etc)

<strategy>  Name of Strategy

END
}

# process options
die() { echo "$*" >&2; exit 2; }  # complain to STDERR and exit with error
needs_arg() { if [ -z "$OPTARG" ]; then die "No arg for --$OPT option"; fi; }

while getopts :c:e:j:l:n:s:t:-: OPT; do
  # support long options: https://stackoverflow.com/a/28466267/519360
  if [ "$OPT" = "-" ]; then   # long option: reformulate OPT and OPTARG
    OPT="${OPTARG%%=*}"       # extract long option name
    OPTARG="${OPTARG#$OPT}"   # extract long option argument (may be empty)
    OPTARG="${OPTARG#=}"      # if long option argument, remove assigning `=`
  fi
  case "$OPT" in
    c | config )     needs_arg; config_file="$OPTARG" ;;
    e | epochs )     needs_arg; epochs="$OPTARG" ;;
    l | loss )       needs_arg; loss="$OPTARG" ;;
    j | jobs )       needs_arg; jarg="-j $OPTARG" ;;
    n | ndays )      needs_arg; num_days="$OPTARG"; timerange="$(date -j -v-${num_days}d +"%Y%m%d")-" ;;
    s | spaces )     needs_arg; spaces="${OPTARG}" ;;
    t | timeframe )  needs_arg; timerange="$OPTARG" ;;
    ??* )            show_usage; die "Illegal option --$OPT" ;;  # bad long option
    ? )              show_usage; die "Illegal option --$OPT" ;;  # bad short option (error reported via getopts)
  esac
done
shift $((OPTIND-1)) # remove parsed options and args from $@ list


if [[ $# -ne 2 ]] ; then
  echo "ERR: Missing arguments"
  show_usage
  exit 0
fi

exchange=$1
strategy=$2

strat_dir="user_data/strategies"
exchange_dir="${strat_dir}/${exchange}"
strat_file="${exchange_dir}/${strategy}.py"

if [ -z "${config_file}" ] ; then
  config_file="${exchange_dir}/config_${exchange}.json"
fi

if [ ! -f ${config_file} ]; then
    echo "config file not found: ${config_file}"
    exit 0
fi

if [ ! -d ${exchange_dir} ]; then
    echo "Strategy dir not found: ${exchange_dir}"
    exit 0
fi

if [ ! -f  ${strat_file} ]; then
    echo "Strategy file file not found: ${strat_file}"
    exit 0
fi

echo ""
echo "Using config file: ${config_file} and Strategy dir: ${exchange_dir}"
echo ""

# set up path
oldpath=${PYTHONPATH}
export PYTHONPATH="./${exchange_dir}:./${strat_dir}:${PYTHONPATH}"

today=`date`
echo $today
echo "Testing strategy:$strategy for exchange:$exchange..."


cmd="freqtrade backtesting --timerange=${timerange} -c ${config_file} --strategy-path ${exchange_dir} --strategy-list ${strategy}"
echo ${cmd}
eval ${cmd}

echo ""

# restore PYTHONPATH
export PYTHONPATH="${oldpath}"

