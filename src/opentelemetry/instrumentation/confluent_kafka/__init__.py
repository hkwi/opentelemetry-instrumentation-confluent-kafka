import confluent_kafka
import wrapt
import weakref

from opentelemetry import trace, context, propagators
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

class ProducerProxy(wrapt.ObjectProxy):
	def __init__(self, instance):
		super(ProducerProxy, self).__init__(instance)
		self._self_instance = instance
	
	def produce(self, *args, **kwargs):
		tracer = trace.get_tracer(__name__)
		with tracer.start_as_current_span("confluent_kafka.produce", kind=trace.SpanKind.PRODUCER) as span:
			a = list(args)
			k = kwargs.copy()
			
			argmap = dict(
				topic=0,
				value=1,
				key=2,
				partition=3,
				timestamp=5
			)
			for name, pos in argmap.items():
				if name in kwargs:
					span.set_attribute("kafka.%s" % name, kwargs[name])
				elif len(args) > pos:
					span.set_attribute("kafka.%s" % name, args[pos])
			
			if "headers" in kwargs:
				headers = k["headers"] = kwargs["headers"].copy()
			elif len(args) > 7:
				headers = a[7] = args[7].copy()
			else:
				headers = k["headers"] = {}
			
			propagators.inject(type(headers).__setitem__, headers)
			return self._self_instance.produce(*a, **k)

@wrapt.decorator
def wrap_producer(wrapped, instance, args, kwargs):
	return ProducerProxy(wrapped(*args, **kwargs))

def get_from_messge(msg, key):
	headers = msg.headers()
	if headers:
		def dec(v):
			try:
				return v.decode("UTF-8")
			except:
				return v
		return [dec(v) for k,v in headers if k == key]
	else:
		return []

def set_span_from_message(span, msg):
	for name in ("topic","timestamp","key","value","partition","offset","error"):
		v = getattr(msg, name)()
		if v is not None:
			span.set_attribute("kafka.%s" % name, v)

class ConsumerProxy(wrapt.ObjectProxy):
	def __init__(self, instance):
		super(ConsumerProxy, self).__init__(instance)
		self._self_instance = instance
		self._self_span = None
	
	def _self_release_context(self):
		if self._self_span:
			self._self_span.end()
			self._self_span = None
	
	def consume(self, *args, **kwargs):
		self._self_release_context()
		msgs = self._self_instance.consume(*args, **kwargs)
		if msgs:
			msg = msgs[0]
			ctx = propagators.extract(get_from_messge, msg)
			self._self_span = trace.get_tracer(__name__).start_span(
				name="confluent_kafka.consume",
				kind=trace.SpanKind.CONSUMER,
				parent=trace.get_current_span(ctx),
			)
			set_span_from_message(self._self_span, msg)
			msg = msgs[0] = MessageProxy(msg)
			weakref.finalize(msg, self._self_release_context)
		return msgs
	
	def poll(self, *args, **kwargs):
		self._self_release_context()
		msg = self._self_instance.poll(*args, **kwargs)
		if msg:
			ctx = propagators.extract(get_from_messge, msg)
			self._self_span = trace.get_tracer(__name__).start_span(
				name="confluent_kafka.poll",
				kind=trace.SpanKind.CONSUMER,
				parent=trace.get_current_span(ctx),
			)
			set_span_from_message(self._self_span, msg)
			msg = MessageProxy(msg)
			weakref.finalize(msg, self._self_release_context)
		return msg

class MessageProxy(wrapt.ObjectProxy):
	def __init__(self, instance):
		super(MessageProxy, self).__init__(instance)

@wrapt.decorator
def wrap_consumer(wrapped, instance, args, kwargs):
	return ConsumerProxy(wrapped(*args, **kwargs))

_raw_producer = confluent_kafka.Producer
_raw_consumer = confluent_kafka.Consumer

class Instrumentor(BaseInstrumentor):
	def _instrument(self, **kwargs):
		confluent_kafka.Producer = wrap_producer(confluent_kafka.Producer)
		confluent_kafka.Consumer = wrap_consumer(confluent_kafka.Consumer)
	
	def _uninstrument(self):
		confluent_kafka.Producer = _raw_producer
		confluent_kafka.Consumer = _raw_consumer

