#
# Copyright (c) 2021 Airbyte, Inc., all rights reserved.
#

import copy
from typing import Any, Iterator, List, Mapping, MutableMapping, Tuple
from requests import exceptions, codes

from airbyte_cdk import AirbyteLogger
from airbyte_cdk.models import AirbyteMessage, ConfiguredAirbyteCatalog
from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.streams import Stream
from airbyte_cdk.sources.streams.http.auth import TokenAuthenticator
from airbyte_cdk.sources.utils.schema_helpers import split_config

from .api import UNSUPPORTED_BULK_API_SALESFORCE_OBJECTS, UNSUPPORTED_FILTERING_STREAMS, Salesforce
from .streams import BulkIncrementalSalesforceStream, BulkSalesforceStream, IncrementalSalesforceStream, SalesforceStream


class SourceSalesforce(AbstractSource):
    @staticmethod
    def _get_sf_object(config: Mapping[str, Any]) -> Salesforce:
        sf = Salesforce(**config)
        sf.login()
        return sf

    def check_connection(self, logger: AirbyteLogger, config: Mapping[str, Any]) -> Tuple[bool, any]:
        try:
            _ = self._get_sf_object(config)
            return True, None
        except exceptions.HTTPError as error:
            error_data = error.response.json()[0]
            error_code = error_data.get("errorCode")
            if error.response.status_code == codes.FORBIDDEN and error_code == "REQUEST_LIMIT_EXCEEDED":
                logger.warn(f"API Call limit is exceeded'. Error message: '{error_data.get('message')}'")
                return False, "API Call limit is exceeded"

    @classmethod
    def generate_streams(
        cls, config: Mapping[str, Any], stream_names: List[str], sf_object: Salesforce, state: Mapping[str, Any] = None
    ) -> List[Stream]:
        """ "Generates a list of stream by their names. It can be used for different tests too"""
        authenticator = TokenAuthenticator(sf_object.access_token)
        streams = []
        for stream_name in stream_names:
            streams_kwargs = {}
            stream_state = state.get(stream_name, {}) if state else {}

            selected_properties = sf_object.generate_schema(stream_name).get("properties", {})
            # Salesforce BULK API currently does not support loading fields with data type base64 and compound data
            properties_not_supported_by_bulk = {
                key: value for key, value in selected_properties.items() if value.get("format") == "base64" or "object" in value["type"]
            }

            if stream_state or stream_name in UNSUPPORTED_BULK_API_SALESFORCE_OBJECTS or properties_not_supported_by_bulk:
                # Use REST API
                full_refresh, incremental = SalesforceStream, IncrementalSalesforceStream
            else:
                # Use BULK API
                full_refresh, incremental = BulkSalesforceStream, BulkIncrementalSalesforceStream
                streams_kwargs["wait_timeout"] = config.get("wait_timeout")

            json_schema = sf_object.generate_schema(stream_name)
            pk, replication_key = sf_object.get_pk_and_replication_key(json_schema)
            streams_kwargs.update(dict(sf_api=sf_object, pk=pk, stream_name=stream_name, schema=json_schema, authenticator=authenticator))
            if replication_key and stream_name not in UNSUPPORTED_FILTERING_STREAMS:
                streams.append(incremental(**streams_kwargs, replication_key=replication_key, start_date=config.get("start_date")))
            else:
                streams.append(full_refresh(**streams_kwargs))

        return streams

    def streams(self, config: Mapping[str, Any], catalog: ConfiguredAirbyteCatalog = None, state: Mapping[str, Any] = None) -> List[Stream]:
        sf = self._get_sf_object(config)
        stream_names = sf.get_validated_streams(config=config, catalog=catalog)
        return self.generate_streams(config, stream_names, sf, state=state)

    def read(
        self, logger: AirbyteLogger, config: Mapping[str, Any], catalog: ConfiguredAirbyteCatalog, state: MutableMapping[str, Any] = None
    ) -> Iterator[AirbyteMessage]:
        """
        Overwritten to dynamically receive only those streams that are necessary for reading for significant speed gains
        (Salesforce has a strict API limit on requests).
        """
        connector_state = copy.deepcopy(state or {})
        config, internal_config = split_config(config)
        # get the streams once in case the connector needs to make any queries to generate them
        logger.info("Starting generating streams")
        stream_instances = {s.name: s for s in self.streams(config, catalog=catalog, state=state)}
        logger.info(f"Starting syncing {self.name}")
        self._stream_to_instance_map = stream_instances
        for configured_stream in catalog.streams:
            stream_instance = stream_instances.get(configured_stream.stream.name)
            if not stream_instance:
                raise KeyError(
                    f"The requested stream {configured_stream.stream.name} was not found in the source. Available streams: {stream_instances.keys()}"
                )

            try:
                yield from self._read_stream(
                    logger=logger,
                    stream_instance=stream_instance,
                    configured_stream=configured_stream,
                    connector_state=connector_state,
                    internal_config=internal_config,
                )
                # TODO if got 403 rate limit, finish the sync with success
            except Exception as e:
                logger.exception(f"Encountered an exception while reading stream {self.name}")
                raise e

        logger.info(f"Finished syncing {self.name}")
