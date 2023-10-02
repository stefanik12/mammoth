
# Installation

## Puhti / Mahti

### Install
In the login node, create a directory in the `projappl` linked to our project to host the shared python dependencies, and install the code base & dependencies there:

<!-- TODO: modify project name  -->
```
# where to install the necessary python packages
ENV_DIR="/projappl/project_2005099/test"
# where the codebase was copied to
CODE_DIR="/scratch/project_2005099/path/to/OpenNMT-py-v2"

# set up variables & modules
module load pytorch
mkdir -p $ENV_DIR
export PYTHONUSERBASE=$ENV_DIR

#install dependencies
cd $CODE_DIR
pip3 install -e . --user

# optionally, to make sure that other people can access this install:
chmod -R 777 $ENV_DIR
chmod -R 777 $CODE_DIR
```

### Run
In slurm job scripts, update environment variables to get python to run your code properly:

```
ENV_DIR="/projappl/project_2005099/test"
CODE_DIR="/scratch/project_2005099/path/to/OpenNMT-py-v2"

module load pytorch
export PYTHONUSERBASE=$ENV_DIR
# note: this overwrites the path, you can also try appending this subdirectory instead
export PYTHONPATH=$ENV_DIR/lib/python3.9/site-packages/

srun python3 -u $CODE_DIR/train.py ...
```


## LUMI

### Install 
1. start an interactive session `srun --account="$PROJECT" --partition=dev-g --ntasks=1 --gres=gpu:mi250:1 --time=2:00:00 --mem=25G --pty bash`
2. Load modules: 
    
    ```bash
    module load cray-python
    module load LUMI/22.08 partition/G rocm/5.2.3
    
    module use /pfs/lustrep2/projappl/project_462000125/samantao-public/mymodules
    module load aws-ofi-rccl/rocm-5.2.3
    ```
3. Create virtual environment `python -m venv your_vevn_name` and activate it `source your_venv_name/bin/activate`. 

4. Install pytorch: `python -m pip install --upgrade torch==1.13.1+rocm5.2 --extra-index-url https://download.pytorch.org/whl/rocm5.2`

5. Install mammoth 
    
    ```bash
    cd /pfs/lustrep1/projappl/${PROJECT}/${USER}/OpenNMT-py-v2
    pip3 install -e .
    pip3 install sentencepiece==0.1.97 sacrebleu==2.3.1
    ```

### Run

TODO