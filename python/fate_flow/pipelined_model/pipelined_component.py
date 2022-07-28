#
#  Copyright 2022 The FATE Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the 'License');
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an 'AS IS' BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import hashlib
from pathlib import Path
from zipfile import ZipFile

from ruamel import yaml

from fate_arch.common.base_utils import json_dumps, json_loads

from fate_flow.db.db_models import DB, PipelineComponentMeta
from fate_flow.db.db_utils import bulk_insert_into_db
from fate_flow.model import Locker
from fate_flow.settings import TEMP_DIRECTORY
from fate_flow.utils.base_utils import get_fate_flow_directory
from fate_flow.utils.log_utils import getLogger


LOGGER = getLogger()


class PipelinedComponent(Locker):

    def __init__(self, *, role=None, party_id=None, model_id=None, party_model_id=None, model_version):
        if party_model_id is None:
            self.role = role
            self.party_id = party_id
            self.model_id = model_id
            self.party_model_id = f'{role}#{party_id}#{model_id}'
        else:
            self.role, self.party_id, self.model_id = party_model_id.split('#', 2)
            self.party_model_id = party_model_id

        self.model_version = model_version

        self.model_path = Path(get_fate_flow_directory('model_local_cache'), self.party_model_id, self.model_version)
        self.define_meta_path = self.model_path / 'define' / 'define_meta.yaml'
        self.variables_data_path = self.model_path / 'variables' / 'data'
        self.run_parameters_path = self.model_path / 'run_parameters'
        self.checkpoint_path = self.model_path / 'checkpoint'

        self.query_args = (
            PipelineComponentMeta.f_model_id == self.model_id,
            PipelineComponentMeta.f_model_version == self.model_version,
            PipelineComponentMeta.f_role == self.role,
            PipelineComponentMeta.f_party_id == self.party_id,
        )

        super().__init__(self.model_path)

    def exists(self, component_name):
        query = self.get_define_meta_from_db(PipelineComponentMeta.f_component_name == component_name)
        if not query:
            raise ValueError(f'The define_meta data of {component_name} not found in database.')

        for row in query:
            variables_data_path = self.variables_data_path / row.f_component_name / row.f_model_alias
            for model_name, buffer_name in row.f_model_proto_index.items():
                if not (variables_data_path / model_name).is_file():
                    return False

        return True

    def get_define_meta_from_file(self):
        return yaml.load(self.define_meta_path.read_text('utf-8'))

    @DB.connection_context()
    def get_define_meta_from_db(self, *query_args):
        return tuple(PipelineComponentMeta.select().where(*self.query_args, *query_args))

    def rearrange_define_meta(self, data):
        define_meta = {
            'component_define': {},
            'model_proto': {},
        }

        for row in data:
            define_meta['component_define'][row.f_component_name] = {'module_name': row.f_component_module_name}
            if row.f_component_name not in define_meta['model_proto']:
                define_meta['model_proto'][row.f_component_name] = {}
            define_meta['model_proto'][row.f_component_name][row.f_model_alias] = row.f_model_proto_index

        return define_meta

    def get_define_meta(self):
        query = self.get_define_meta_from_db()
        return self.rearrange_define_meta(query) if query else self.get_define_meta_from_file()

    @DB.connection_context()
    def save_define_meta(self, component_name, component_module_name, model_alias, model_proto_index, run_parameters):
        PipelineComponentMeta.insert(
            f_model_id=self.model_id,
            f_model_version=self.model_version,
            f_role=self.role,
            f_party_id=self.party_id,
            f_component_name=component_name,
            f_component_module_name=component_module_name,
            f_model_alias=model_alias,
            f_model_proto_index=model_proto_index,
            f_run_parameters=run_parameters,
        ).execute()

    def save_define_meta_from_db_to_file(self):
        query = self.get_define_meta_from_db()
        if not query:
            raise ValueError(f'No define_meta data in database.')

        for row in query:
            run_parameters_path = self.get_run_parameters_path(row.f_component_name)
            run_parameters_path.parent.mkdir(parents=True, exist_ok=True)

            with run_parameters_path.open('x', encoding='utf-8') as f:
                f.write(json_dumps(row.run_parameters))

        self.define_meta_path.parent.mkdir(parents=True, exist_ok=True)

        with self.define_meta_path.open('x', encoding='utf-8') as f:
            yaml.dump(self.rearrange_define_meta(query), f, Dumper=yaml.RoundTripDumper)

    def save_define_meta_from_file_to_db(self):
        with DB.connection_context():
            count = PipelineComponentMeta.select().where(*self.query_args).count()
        if count > 0:
            raise ValueError(f'The define_meta data already exists in database.')

        define_meta = self.get_define_meta_from_file()
        run_parameters = self.get_run_parameters_from_files()

        insert = []
        for component_name, component_define in define_meta['component_define'].items():
            for model_alias, model_proto_index in define_meta['model_proto'][component_name].items():
                row = {
                    'f_model_id': self.model_id,
                    'f_model_version': self.model_version,
                    'f_role': self.role,
                    'f_party_id': self.party_id,
                    'f_component_name': component_name,
                    'f_component_module_name': component_define['module_name'],
                    'f_model_alias': model_alias,
                    'f_model_proto_index': model_proto_index,
                    'f_run_parameters': run_parameters.get(component_name, {}),
                }
                insert.append(row)

        bulk_insert_into_db(PipelineComponentMeta, insert, LOGGER)

    def replicate_define_meta(self, modification, query_args=None):
        query = self.get_define_meta_from_db(*query_args)
        if not query:
            raise ValueError(f'Filtered define_meta data not found.')

        insert = []
        for row in query:
            row = row.to_dict()
            del row['id']
            row = {
                key[2:] if key.startswith('f_') else key: value
                for key, value in row.items()
                if key != 'id'
            }

            row.update(modification)
            insert.append(row)

        bulk_insert_into_db(PipelineComponentMeta, insert, LOGGER)

    def get_run_parameters_path(self, component_name):
        return self.run_parameters_path / component_name / 'run_parameters.json'

    def get_run_parameters_from_files(self):
        if not self.run_parameters_path.is_dir():
            return {}

        return {
            path.name: json_loads(self.get_run_parameters_path(path.name).read_text('utf-8'))
            for path in self.run_parameters_path.iterdir()
        }

    def get_run_parameters(self):
        query = self.get_define_meta_from_db()
        return {
            row.f_component_name: row.f_run_parameters
            for row in query
        } if query else self.get_run_parameters_from_files()

    def get_archive_path(self, component_name):
        return Path(TEMP_DIRECTORY, f'{self.party_model_id}_{self.model_version}_{component_name}.zip')

    def walk_component(self, zip_file, dir_path: Path):
        for path in dir_path.iterdir():
            if path.is_dir():
                self.walk_component(zip_file, path)
            else:
                zip_file.write(path, path.relative_to(self.model_path))

    def pack_component(self, component_name):
        filename = self.get_archive_path(component_name)

        with self.lock:
            with ZipFile(filename, 'w') as zip_file:
                self.walk_component(zip_file, self.variables_data_path / component_name)
                self.walk_component(zip_file, self.checkpoint_path / component_name)

            hash_ = hashlib.sha256(filename.read_bytes()).hexdigest()

        return filename, hash_

    def unpack_component(self, component_name, hash_=None):
        filename = self.get_archive_path(component_name)

        with self.lock:
            if hash_ is not None:
                sha256 = hashlib.sha256(filename.read_bytes()).hexdigest()

                if hash_ != sha256:
                    raise ValueError(f'Model archive hash mismatch. path: {filename} expected: {hash_} actual: {sha256}')

            with ZipFile(filename, 'r') as zip_file:
                zip_file.extractall(self.model_path)
