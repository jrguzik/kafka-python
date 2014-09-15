from collections import namedtuple
from copy import deepcopy
import logging
import sys
import time

import six

from kafka.client import KafkaClient
from kafka.common import (
    OffsetFetchRequest, OffsetCommitRequest, OffsetRequest, FetchRequest,
    check_error, NotLeaderForPartitionError, UnknownTopicOrPartitionError,
    OffsetOutOfRangeError, RequestTimedOutError, KafkaMessage, ConsumerTimeout,
    FailedPayloadsError, KafkaUnavailableError, KafkaConfigurationError
)

logger = logging.getLogger(__name__)

OffsetsStruct = namedtuple("OffsetsStruct", ["fetch", "highwater", "commit", "task_done"])

DEFAULT_CONSUMER_CONFIG = {
    'client_id': __name__,
    'group_id': None,
    'metadata_broker_list': None,
    'socket_timeout_ms': 30 * 1000,
    'fetch_message_max_bytes': 1024 * 1024,
    'auto_offset_reset': 'largest',
    'fetch_min_bytes': 1,
    'fetch_wait_max_ms': 100,
    'refresh_leader_backoff_ms': 200,
    'deserializer_class': lambda msg: msg,
    'auto_commit_enable': False,
    'auto_commit_interval_ms': 60 * 1000,
    'consumer_timeout_ms': -1,

    # Currently unused
    'socket_receive_buffer_bytes': 64 * 1024,
    'num_consumer_fetchers': 1,
    'default_fetcher_backoff_ms': 1000,
    'queued_max_message_chunks': 10,
    'rebalance_max_retries': 4,
    'rebalance_backoff_ms': 2000,
}


class KafkaConsumer(object):
  """
  A simpler kafka consumer

  ```
  # A very basic 'tail' consumer, with no stored offset management
  kafka = KafkaConsumer('topic1')
  for m in kafka:
    print m

  # Alternate interface: next() 
  while True:
    print kafka.next()

  # Alternate interface: batch iteration
  while True:
    for m in kafka.fetch_messages():
      print m
    print "Done with batch - let's do another!"
  ```

  ```
  # more advanced consumer -- multiple topics w/ auto commit offset management
  kafka = KafkaConsumer('topic1', 'topic2',
                        group_id='my_consumer_group',
                        auto_commit_enable=True,
                        auto_commit_interval_ms=30 * 1000,
                        auto_offset_reset='smallest')

  # Infinite iteration
  for m in kafka:
    process_message(m)
    kafka.task_done(m)

  # Alternate interface: next()
  while True:
    m = kafka.next()
    process_message(m)
    kafka.task_done(m)

  # Batch process interface does not auto_commit!
  while True:
    for m in kafka.fetch_messages():
      process_message(m)
      kafka.task_done(m)
    kafka.commit()
  ```

  messages (m) are namedtuples with attributes:
    m.topic: topic name (str)
    m.partition: partition number (int)
    m.offset: message offset on topic-partition log (int)
    m.key: key (bytes - can be None)
    m.value: message (output of deserializer_class - default is event object)

  Configuration settings can be passed to constructor,
  otherwise defaults will be used:
    client_id='kafka.consumer.XXX',
    group_id=None,
    fetch_message_max_bytes=1024*1024,
    fetch_min_bytes=1,
    fetch_wait_max_ms=100,
    refresh_leader_backoff_ms=200,
    metadata_broker_list=None,
    socket_timeout_ms=30*1000,
    auto_offset_reset='largest',
    deserializer_class=Event.from_bytes,
    auto_commit_enable=False,
    auto_commit_interval_ms=60 * 1000,
    consumer_timeout_ms=-1


  Configuration parameters are described in more detail at
  http://kafka.apache.org/documentation.html#highlevelconsumerapi
  """

  def __init__(self, *topics, **configs):
    self.configure(**configs)
    self.set_topic_partitions(*topics)
    self._msg_iter = None

  def _get_commit_offsets(self):
    logger.info("Consumer fetching stored offsets")
    for topic_partition in self._topics:
      (resp,) = self._client.send_offset_fetch_request(
          self._config['group_id'],
          [OffsetFetchRequest(topic_partition[0], topic_partition[1])],
          fail_on_error=False)
      try:
        check_error(resp)
      # API spec says server wont set an error here
      # but 0.8.1.1 does actually...
      except UnknownTopicOrPartitionError:
        pass

      # -1 offset signals no commit is currently stored
      if resp.offset == -1:
        self._offsets.commit[topic_partition] = None

      # Otherwise we committed the stored offset
      # and need to fetch the next one
      else:
        self._offsets.commit[topic_partition] = resp.offset

  def _reset_highwater_offsets(self):
    for topic_partition in self._topics:
      self._offsets.highwater[topic_partition] = None

  def _reset_task_done_offsets(self):
    for topic_partition in self._topics:
      self._offsets.task_done[topic_partition] = None

  def __repr__(self):
    return '<KafkaConsumer topics=(%s)>' % ', '.join(["%s-%d" % topic_partition
                                                      for topic_partition in
                                                      self._topics])

  def __iter__(self):
    return self

  def next(self):
    self._set_consumer_timeout_start()
    while True:

      # Fetch a new batch if needed
      if self._msg_iter is None:
        self._msg_iter = self.fetch_messages()

      # Check for auto-commit
      if self._should_auto_commit():
        self.commit()

      try:
        return self._msg_iter.next()

      # Handle batch completion
      except StopIteration:
        self._msg_iter = None

      self._check_consumer_timeout()

  def offsets(self, group=None):
    if not group:
      return {
        'fetch': self.offsets('fetch'),
        'commit': self.offsets('commit'),
        'task_done': self.offsets('task_done'),
        'highwater': self.offsets('highwater')
      }
    else:
      return dict(deepcopy(getattr(self._offsets, group)))

  def task_done(self, message):
    """
    Mark a fetched message as consumed.
    Offsets for messages marked as "task_done" will be stored back
    to the kafka cluster for this consumer group on commit()
    """
    topic_partition = (message.topic, message.partition)
    offset = message.offset

    # Warn on non-contiguous offsets
    prev_done = self._offsets.task_done[topic_partition]
    if prev_done is not None and offset != (prev_done + 1):
      logger.warning('Marking task_done on a non-continuous offset: %d != %d + 1',
                     offset, prev_done)

    # Warn on smaller offsets than previous commit
    # "commit" offsets are actually the offset of the next # message to fetch.
    # so task_done should be compared with (commit - 1)
    prev_done = (self._offsets.commit[topic_partition] - 1)
    if prev_done is not None and (offset <= prev_done):
      logger.warning('Marking task_done on a previously committed offset?: %d <= %d',
                     offset, prev_done)

    self._offsets.task_done[topic_partition] = offset

  def _should_auto_commit(self):
    if not self._config['auto_commit_enable']:
      return False

    if not self._next_commit:
      return False

    return (time.time() >= self._next_commit)

  def _set_next_auto_commit_time(self):
    self._next_commit = time.time() + (self._config['auto_commit_interval_ms'] / 1000.0)

  def commit(self):
    """
    Store consumed message offsets (marked via task_done())
    to kafka cluster for this consumer_group.

    Note -- this functionality requires server version >=0.8.1.1
    see https://cwiki.apache.org/confluence/display/KAFKA/A+Guide+To+The+Kafka+Protocol#AGuideToTheKafkaProtocol-OffsetCommit/FetchAPI
    """
    if not self._config['group_id']:
      logger.warning('Cannot commit without a group_id!')
      raise KafkaConfigurationError('Attempted to commit offsets without a configured consumer group (group_id)')

    # API supports storing metadata with each commit
    # but for now it is unused
    metadata = ''

    offsets = self._offsets.task_done
    commits = []
    for topic_partition, task_done_offset in offsets.iteritems():

      # Skip if None
      if task_done_offset is None:
        continue

      # Commit offsets as the next offset to fetch
      # which is consistent with the Java Client
      # task_done is marked by messages consumed,
      # so add one to mark the next message for fetching
      commit_offset = (task_done_offset + 1)

      # Skip if no change from previous committed
      if commit_offset == self._offsets.commit[topic_partition]:
        continue

      commits.append(OffsetCommitRequest(topic_partition[0], topic_partition[1], commit_offset, metadata))

    if commits:
      logger.info('committing consumer offsets to group %s', self._config['group_id'])
      resps = self._client.send_offset_commit_request(self._config['group_id'],
                                                     commits,
                                                     fail_on_error=False)

      for r in resps:
        check_error(r)
        topic_partition = (r.topic, r.partition)
        task_done = self._offsets.task_done[topic_partition]
        self._offsets.commit[topic_partition] = (task_done + 1)

      if self._config['auto_commit_enable']:
        self._set_next_auto_commit_time()

      return True

    else:
      logger.info('No new offsets found to commit in group %s', self._config['group_id'])
      return False

  def configure(self, **configs):
    self._config = {}
    for key in DEFAULT_CONSUMER_CONFIG:
      self._config[key] = configs.pop(key, DEFAULT_CONSUMER_CONFIG[key])

    if configs:
      raise KafkaConfigurationError('Unknown configuration key(s): ' +
                                    str(list(configs.keys())))

    if self._config['auto_commit_enable']:
      if not self._config['group_id']:
        raise KafkaConfigurationError('KafkaConsumer configured to auto-commit without required consumer group (group_id)')

    # Check auto-commit configuration
    if self._config['auto_commit_enable']:
      logger.info("Configuring consumer to auto-commit offsets")
      self._set_next_auto_commit_time()

    if self._config['metadata_broker_list'] is None:
      raise KafkaConfigurationError('metadata_broker_list required to '
                                    'configure KafkaConsumer')

    self._client = KafkaClient(self._config['metadata_broker_list'],
                              client_id=self._config['client_id'],
                              timeout=(self._config['socket_timeout_ms'] / 1000.0))

  def set_topic_partitions(self, *topics):
    """
    Set the topic/partitions to consume
    Optionally specify offsets to start from

    Accepts types:
    str: topic name (will consume all available partitions)
    tuple: (topic, partition)
    dict: { topic: partition }
          { topic: [partition list] }
          { topic: (partition tuple,) }

    Optionally, offsets can be specified directly:
    tuple: (topic, partition, offset)
    dict:  { (topic, partition): offset, ... }

    Ex:
    kafka = KafkaConsumer()

    # Consume topic1-all; topic2-partition2; topic3-partition0
    kafka.set_topic_partitions("topic1", ("topic2", 2), {"topic3": 0})

    # Consume topic1-0 starting at offset 123, and topic2-1 at offset 456
    # using tuples --
    kafka.set_topic_partitions(("topic1", 0, 123), ("topic2", 1, 456))

    # using dict --
    kafka.set_topic_partitions({ ("topic1", 0): 123, ("topic2", 1): 456 })
    """
    self._topics = []
    self._client.load_metadata_for_topics()

    # Setup offsets
    self._offsets = OffsetsStruct(fetch=dict(),
                                  commit=dict(),
                                  highwater=dict(),
                                  task_done=dict())

    # Handle different topic types
    for arg in topics:

      # Topic name str -- all partitions
      if isinstance(arg, six.string_types):
        topic = arg
        for partition in self._client.get_partition_ids_for_topic(arg):
          self._consume_topic_partition(topic, partition)

      # (topic, partition [, offset]) tuple
      elif isinstance(arg, tuple):
        (topic, partition) = arg[0:2]
        if len(arg) == 3:
          offset = arg[2]
          self._offsets.fetch[(topic, partition)] = offset
        self._consume_topic_partition(topic, partition)

      # { topic: partitions, ... } dict
      elif isinstance(arg, dict):
        for key, value in arg.iteritems():

          # key can be string (a topic)
          if isinstance(key, six.string_types):

            # topic: partition
            if isinstance(value, int):
              self._consume_topic_partition(key, value)

            # topic: [ partition1, partition2, ... ]
            elif isinstance(value, (list, tuple)):
              for partition in value:
                self._consume_topic_partition(key, partition)
            else:
              raise KafkaConfigurationError('Unknown topic type (dict key must be '
                                            'int or list/tuple of ints)')

          # (topic, partition): offset
          elif isinstance(key, tuple):
            self._consume_topic_partition(*key)
            self._offsets.fetch[key] = value

      else:
        raise KafkaConfigurationError('Unknown topic type (%s)' % type(arg))

    # If we have a consumer group, try to fetch stored offsets
    if self._config['group_id']:
      self._get_commit_offsets()

    # Update missing fetch/commit offsets
    for topic_partition in self._topics:

      # Commit offsets default is None
      if topic_partition not in self._offsets.commit:
        self._offsets.commit[topic_partition] = None

      # Skip if we already have a fetch offset from user args
      if topic_partition not in self._offsets.fetch:

        # Fetch offsets default is (1) commit
        if self._offsets.commit[topic_partition] is not None:
          self._offsets.fetch[topic_partition] = self._offsets.commit[topic_partition]

        # or (2) auto reset
        else:
          self._offsets.fetch[topic_partition] = self._reset_partition_offset(topic_partition)

    # highwater marks (received from server on fetch response)
    # and task_done (set locally by user)
    # should always get initialized to None
    self._reset_highwater_offsets()
    self._reset_task_done_offsets()

  def _consume_topic_partition(self, topic, partition):
    if not isinstance(topic, six.string_types):
      raise KafkaConfigurationError('Unknown topic type (%s) '
                                    '-- expected string' % type(topic))
    if not isinstance(partition, int):
      raise KafkaConfigurationError('Unknown partition type (%s) '
                                    '-- expected int' % type(partition))

    if topic not in self._client.topic_partitions:
      raise UnknownTopicOrPartitionError("Topic %s not found in broker metadata" % topic)
    if partition not in self._client.get_partition_ids_for_topic(topic):
      raise UnknownTopicOrPartitionError("Partition %d not found in Topic %s "
                                         "in broker metadata" % (partition, topic))
    logger.info("Configuring consumer to fetch topic '%s', partition %d", topic, partition)
    self._topics.append((topic, partition))

  def fetch_messages(self):

    max_bytes = self._config['fetch_message_max_bytes']
    max_wait_time = self._config['fetch_wait_max_ms']
    min_bytes = self._config['fetch_min_bytes']

    fetches = []
    offsets = self._offsets.fetch
    for topic_partition, offset in offsets.iteritems():
      fetches.append(FetchRequest(topic_partition[0], topic_partition[1], offset, max_bytes))

    # client.send_fetch_request will collect topic/partition requests by leader
    # and send each group as a single FetchRequest to the correct broker
    try:
      responses = self._client.send_fetch_request(fetches,
                                                 max_wait_time=max_wait_time,
                                                 min_bytes=min_bytes,
                                                 fail_on_error=False)
    except FailedPayloadsError:
      logger.warning('FailedPayloadsError attempting to fetch data from kafka')
      self._refresh_metadata_on_error()
      return

    for resp in responses:
      topic_partition = (resp.topic, resp.partition)
      try:
        check_error(resp)
      except OffsetOutOfRangeError:
        logger.warning('OffsetOutOfRange: topic %s, partition %d, offset %d '
                       '(Highwatermark: %d)',
                       resp.topic, resp.partition,
                       offsets[topic_partition], resp.highwaterMark)
        # Reset offset
        self._offsets.fetch[topic_partition] = self._reset_partition_offset(topic_partition)
        continue

      except NotLeaderForPartitionError:
        logger.warning("NotLeaderForPartitionError for %s - %d. "
                       "Metadata may be out of date",
                       resp.topic, resp.partition)
        self._refresh_metadata_on_error()
        continue

      except RequestTimedOutError:
        logger.warning("RequestTimedOutError for %s - %d",
                       resp.topic, resp.partition)
        continue

      # Track server highwater mark
      self._offsets.highwater[topic_partition] = resp.highwaterMark

      # Yield each message
      # Kafka-python could raise an exception during iteration
      # we are not catching -- user will need to address
      for (offset, message) in resp.messages:
        # deserializer_class could raise an exception here
        msg = KafkaMessage(resp.topic,
                           resp.partition,
                           offset, message.key,
                           self._config['deserializer_class'](message.value))

        # Only increment fetch offset if we safely got the message and deserialized
        self._offsets.fetch[topic_partition] = offset + 1

        # Then yield to user
        yield msg

  def _reset_partition_offset(self, topic_partition):
    (topic, partition) = topic_partition
    LATEST = -1
    EARLIEST = -2

    RequestTime = None
    if self._config['auto_offset_reset'] == 'largest':
      RequestTime = LATEST
    elif self._config['auto_offset_reset'] == 'smallest':
      RequestTime = EARLIEST
    else:

      # Let's raise an reasonable exception type if user calls
      # outside of an exception context
      if sys.exc_info() == (None, None, None):
        raise OffsetOutOfRangeError('Cannot reset partition offsets without a '
                                    'valid auto_offset_reset setting '
                                    '(largest|smallest)')

      # Otherwise we should re-raise the upstream exception
      # b/c it typically includes additional data about
      # the request that triggered it, and we do not want to drop that
      raise

    (offset, ) = self.get_partition_offsets(topic, partition, RequestTime,
                                            num_offsets=1)
    return offset

  def get_partition_offsets(self, topic, partition, request_time, num_offsets):
    reqs = [OffsetRequest(topic, partition, request_time, num_offsets)]

    (resp,) = self._client.send_offset_request(reqs)

    check_error(resp)

    # Just for sanity..
    # probably unnecessary
    assert resp.topic == topic
    assert resp.partition == partition

    return resp.offsets

  def _refresh_metadata_on_error(self):
    sleep_ms = self._config['refresh_leader_backoff_ms']
    while True:
      logger.info("Sleeping for refresh_leader_backoff_ms: %d", sleep_ms)
      time.sleep(sleep_ms / 1000.0)
      try:
        self._client.load_metadata_for_topics()
      except KafkaUnavailableError:
        logger.warning("Unable to refresh topic metadata... cluster unavailable")
        self._check_consumer_timeout()
      else:
        logger.info("Topic metadata refreshed")
        return

  def _set_consumer_timeout_start(self):
    self._consumer_timeout = False
    if self._config['consumer_timeout_ms'] >= 0:
      self._consumer_timeout = time.time() + (self._config['consumer_timeout_ms'] / 1000.0)

  def _check_consumer_timeout(self):
    if self._consumer_timeout and time.time() > self._consumer_timeout:
      raise ConsumerTimeout('Consumer timed out after %d ms' % + self._config['consumer_timeout_ms'])
