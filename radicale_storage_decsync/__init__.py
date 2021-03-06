#!/usr/bin/env python3

import json
import os
import vobject

import radicale.storage as storage
from libdecsync import Decsync

def _get_attributes_from_path(path):
    sane_path = storage.sanitize_path(path).strip("/")
    attributes = sane_path.split("/")
    if not attributes[0]:
        attributes.pop()
    return attributes

class CollectionHrefMappingsMixin:
    def load_hrefs(self, sync_type):
        if sync_type == "contacts":
            self._suffix = ".vcf"
        else:
            self._suffix = ".ics"
        self._hrefs_path = os.path.join(self._filesystem_path, ".Radicale.hrefs")
        try:
            with open(self._hrefs_path) as f:
                self._hrefs = json.load(f)
        except:
            self._hrefs = {}
        self._uids = {}
        for uid, href in self._hrefs.items():
            self._uids[href] = uid

    def get_href(self, uid):
        return self._hrefs.get(uid, uid + self._suffix)

    def set_href(self, uid, href):
        if href == self.get_href(uid):
            return
        self._hrefs[uid] = href
        self._uids[href] = uid
        with self._atomic_write(self._hrefs_path, "w") as f:
            json.dump(self._hrefs, f)

    def get_uid(self, href):
        return self._uids.get(href, href[:-len(self._suffix)])

class Collection(storage.Collection, CollectionHrefMappingsMixin):
    def __init__(self, path, filesystem_path=None):
        super().__init__(path, filesystem_path=filesystem_path)
        attributes = _get_attributes_from_path(path)
        if len(attributes) == 2:
            decsync_dir = self.__class__.decsync_dir
            sync_type = attributes[1].split("-")[0]
            collection = attributes[1][len(sync_type)+1:]
            own_app_id = Decsync.get_app_id("Radicale")
            self.decsync = Decsync(decsync_dir, sync_type, collection, own_app_id)

            def info_listener(path, datetime, key, value, extra):
                if key == "name":
                    extra._set_meta_key("D:displayname", value, update_decsync=False)
                elif key == "deleted":
                    extra.delete(update_decsync=False)
                elif key == "color":
                    extra._set_meta_key("ICAL:calendar-color", value, update_decsync=False)
                else:
                    raise ValueError("Unknown info key " + key)
            self.decsync.add_listener(["info"], info_listener)

            def resources_listener(path, datetime, key, value, extra):
                if len(path) != 1:
                    raise ValueError("Invalid path " + str(path))
                uid = path[0]
                href = extra.get_href(uid)
                if value is None:
                    if extra.get(href) is not None:
                        extra.delete(href, update_decsync=False)
                else:
                    item = vobject.readOne(value)
                    if sync_type == "contacts":
                        tag = "VADDRESSBOOK"
                    elif sync_type == "calendars":
                        tag = "VCALENDAR"
                    else:
                        raise RuntimeError("Unknown sync type " + sync_type)
                    storage.check_and_sanitize_item(item, uid=uid, tag=tag)
                    extra.upload(href, item, update_decsync=False)
            self.decsync.add_listener(["resources"], resources_listener)

            self.load_hrefs(sync_type)

    @classmethod
    def static_init(cls):
        cls.decsync_dir = os.path.expanduser(cls.configuration.get("storage", "decsync_dir", fallback=""))
        super().static_init()

    @classmethod
    def discover(cls, path, depth="0"):
        collections = list(super().discover(path, depth))
        for collection in collections:
            yield collection

        if depth == "0":
            return

        attributes = _get_attributes_from_path(path)

        if len(attributes) == 0:
            return
        elif len(attributes) == 1:
            known_paths = [collection.path for collection in collections]
            for sync_type in ["contacts", "calendars"]:
                for collection in Decsync.list_collections(cls.decsync_dir, sync_type):
                    child_path = storage.sanitize_path(path + "/" + sync_type + "-" + collection).strip("/")
                    if child_path in known_paths:
                        continue
                    if Decsync.get_static_info(cls.decsync_dir, sync_type, collection, "deleted") == True:
                        continue

                    props = {}
                    if sync_type == "contacts":
                        props["tag"] = "VADDRESSBOOK"
                    elif sync_type == "calendars":
                        props["tag"] = "VCALENDAR"
                        props["C:supported-calendar-component-set"] = "VEVENT"
                    else:
                        raise RuntimeError("Unknown sync type " + sync_type)
                    child = super().create_collection(child_path, props=props)
                    child.decsync.init_stored_entries()
                    child.decsync.execute_stored_entries_for_path(["info"], child)
                    child.decsync.execute_stored_entries_for_path(["resources"], child)
                    yield child
        elif len(attributes) == 2:
            return
        else:
            raise ValueError("Invalid number of attributes")

    @classmethod
    def create_collection(cls, href, items=None, props=None):
        if props is None:
            return super().create_collection(href, items, props)

        if props.get("tag") == "VADDRESSBOOK":
            sync_type = "contacts"
        elif props.get("tag") == "VCALENDAR":
            sync_type = "calendars"
        else:
            raise ValueError("Unknown tag " + props.get("tag"))

        attributes = _get_attributes_from_path(href)
        attributes[-1] = sync_type + "-" + attributes[-1]
        path = "/".join(attributes)
        return super().create_collection(path, items, props)

    def upload(self, href, vobject_item, update_decsync=True):
        item = super().upload(href, vobject_item)
        if update_decsync:
            self.set_href(item.uid, href)
            self.decsync.set_entry(["resources", item.uid], None, item.serialize())
        return item

    def delete(self, href=None, update_decsync=True):
        if update_decsync:
            if href is None:
                self.decsync.set_entry(["info"], "deleted", True)
            else:
                uid = self.get_uid(href)
                self.decsync.set_entry(["resources", uid], None, None)
        super().delete(href)

    def set_meta_all(self, props, update_decsync=True):
        if update_decsync:
            for key, value in props.items():
                if self.get_meta(key) == value:
                    continue
                if key == "D:displayname":
                    self.decsync.set_entry(["info"], "name", value)
                elif key == "ICAL:calendar-color":
                    self.decsync.set_entry(["info"], "color", value)
        super().set_meta_all(props)

    def _set_meta_key(self, key, value, update_decsync=True):
        props = self.get_meta()
        props[key] = value
        self.set_meta_all(props, update_decsync)

    @property
    def etag(self):
        self.decsync.execute_all_new_entries(self)
        return super().etag

    def sync(self, old_token=None):
        self.decsync.execute_all_new_entries(self)
        return super().sync(old_token)
