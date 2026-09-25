import unittest

from ptk import multipath

CONF = """
# Pure recommended
defaults {
    user_friendly_names no
    find_multipaths     yes   # inline comment
}
devices {
    device {
        vendor               "PURE"
        product              "FlashArray"
        path_selector        "service-time 0"
        hardware_handler     "1 alua"
        no_path_retry        0
    }
}
blacklist
{
    device {
        vendor "VMware"
        product "Virtual disk"
    }
}
"""


class MultipathTest(unittest.TestCase):
    def test_parse_nested_and_quoted(self):
        conf = multipath.parse(CONF)
        self.assertEqual(conf.sections("defaults")[0].attrs["user_friendly_names"], "no")
        self.assertEqual(conf.sections("defaults")[0].attrs["find_multipaths"], "yes")
        dev = multipath.find_device(conf, "PURE", "FlashArray")
        self.assertEqual(dev.attrs["hardware_handler"], "1 alua")
        self.assertEqual(dev.attrs["path_selector"], "service-time 0")

    def test_brace_on_next_line(self):
        conf = multipath.parse(CONF)
        self.assertTrue(multipath.is_blacklisted(conf, "VMware", "Virtual disk"))
        self.assertFalse(multipath.is_blacklisted(conf, "PURE", "FlashArray"))

    def test_conf_d_later_stanza_wins(self):
        override = 'devices {\n device {\n vendor "PURE"\n product "FlashArray"\n no_path_retry 10\n }\n}\n'
        conf = multipath.merge([multipath.parse(CONF), multipath.parse(override)])
        self.assertEqual(multipath.find_device(conf, "PURE", "FlashArray").attrs["no_path_retry"], "10")


if __name__ == "__main__":
    unittest.main()
