"""
Stores the current input requirement for the web requests to await their arrival
"""

import datetime
import uuid
from threading import Condition, Thread

from ansi2html import Ansi2HTMLConverter

from meticulous._multiworker import Interaction, multiworker_core

INPUT = 0
CONFIRMATION = 1


class StateHandler(Interaction):
    """
    Records the state to await user requests
    """

    def __init__(self):
        self.alive = False
        self.started_at = datetime.datetime.min
        self.condition = Condition()
        self.messages = []
        self.await_key = None
        self.response_val = None
        self.thread = None

    def response(self):
        """
        Return the current input requirement to the end user
        """
        conv = Ansi2HTMLConverter()
        content = "".join(conv.convert(msg) for msg in self.messages)
        if self.await_key is not None:
            content += self.await_key.get_html()
        return f"<html><body>{content}</body></html>"

    def start(self, target):
        """
        Begin processing
        """
        if self.thread is not None:
            self.stop()
        self.thread = Thread(target=self.run, args=(target,), name="webworker")
        self.thread.start()

    def run(self, target):
        """
        Perform processing
        """
        self.started_at = datetime.datetime.now()
        self.alive = True
        while self.alive:
            multiworker_core(self, target)

    def stop(self):
        """
        Gracefully stop processing
        """
        self.alive = False
        self.thread.join()
        self.thread = None
        self.started_at = datetime.datetime.min

    def get_input(self, message):
        return self.get_await(Input(message))

    def check_quit(self):
        return True

    def get_confirmation(self, message="Do you want to continue", defaultval=True):
        return self.get_await(Confirmation(message, defaultval))

    def get_await(self, key):
        """
        Work out the user response
        """
        with self.condition:
            self.response_val = None
            self.await_key = key
            while self.response_val is None:
                self.condition.wait(10)
        return self.response_val

    def send(self, message):
        self.messages.append(message)


class Awaiter:
    """
    Waiting on some user input
    """

    def __init__(self):
        self.uuid = uuid.uuid4()

    def get_form(self, content):
        """
        Get form submission html
        """
        return f"""
<form method="POST">
<input type="hidden" name="uuid" value="{self.uuid}">
{content}
</form>
"""

    def get_html(self):
        """
        Obtain the request HTML
        """
        raise NotImplementedError()


class Confirmation(Awaiter):
    """
    Yes/No
    """

    def __init__(self, message, defaultval):
        super().__init__()
        self.message = message
        self.defaultval = defaultval

    def get_html(self):
        """
        Obtain the request HTML
        """
        conv = Ansi2HTMLConverter()
        content = conv.convert(self.message)
        buttons = """
<table><tr><td>
<input type="submit" value="Yes" />
</td><td>
<input type="submit" value="No" />
</td></tr></table>
"""
        content += self.get_form(buttons)
        return content


class Input(Awaiter):
    """
    Text Input
    """

    def __init__(self, message):
        super().__init__()
        self.message = message

    def get_html(self):
        """
        Obtain the request HTML
        """
        conv = Ansi2HTMLConverter()
        content = conv.convert(self.message)
        textinput = """
<input type="text" name="textinput" value="" />
<input type="submit" value="Save" />
"""
        content += self.get_form(textinput)
        return content


STATE = StateHandler()
